import _thread
import argparse
import asyncio
import functools
import os
import sys
import time
import wave
from datetime import datetime

import websockets
from flask import request, Flask, render_template
from flask_cors import CORS

from asr import SUPPORT_MODEL
from asr.predict import Predictor
from asr.utils.audio_vad import crop_audio_vad
from asr.utils.utils import add_arguments, print_arguments
from asr.utils.logger import setup_logger

logger = setup_logger(__name__)

parser = argparse.ArgumentParser(description=__doc__)
add_arg = functools.partial(add_arguments, argparser=parser)
add_arg('use_model',        str,    'deepspeech2',        "所使用的模型", choices=SUPPORT_MODEL)
add_arg('feature_method',   str,    'linear',             "音频预处理方法", choices=['linear', 'mfcc', 'fbank'])
add_arg("host",             str,    "0.0.0.0",            "监听主机的IP地址")
add_arg("port",             int,    5000,                 "普通识别服务所使用的端口号")
add_arg("port_stream",      int,    5001,                 "流式识别服务所使用的端口号")
add_arg("save_path",        str,    'dataset/upload/',    "上传音频文件的保存目录")
add_arg('use_gpu',          bool,   True,   "是否使用GPU预测")
add_arg('use_pun',          bool,   False,  "是否给识别结果加标点符号")
add_arg('to_an',            bool,   False,  "是否转为阿拉伯数字")
add_arg('num_predictor',    int,    1,      "多少个预测器，也是就可以同时有多少个用户同时识别")
add_arg('beam_size',        int,    300,    "集束搜索解码相关参数，搜索大小，范围:[5, 500]")
add_arg('alpha',            float,  2.2,    "集束搜索解码相关参数，LM系数")
add_arg('beta',             float,  4.3,    "集束搜索解码相关参数，WC系数")
add_arg('cutoff_prob',      float,  0.99,   "集束搜索解码相关参数，剪枝的概率")
add_arg('cutoff_top_n',     int,    40,     "集束搜索解码相关参数，剪枝的最大值")
add_arg('vocab_path',       str,    'dataset/vocabulary.txt',    "数据集的词汇表文件路径")
add_arg('model_path',       str,    'models/{}_{}/inference.pt', "导出的预测模型文件路径")
add_arg('pun_model_dir',    str,    'models/pun_models/',        "加标点符号的模型文件夹路径")
add_arg('lang_model_path',  str,    'lm/zh_giga.no_cna_cmn.prune01244.klm',    "集束搜索解码相关参数，语言模型文件路径")
add_arg('decoder',          str,    'ctc_beam_search',    "结果解码方法",   choices=['ctc_beam_search', 'ctc_greedy'])
args = parser.parse_args()
print_arguments(args)

app = Flask(__name__, template_folder="templates", static_folder="static", static_url_path="/")
# 允许跨越访问
CORS(app)


# 创建多个预测器，实时语音识别所以要这样处理
predictors = []
for _ in range(args.num_predictor):
    predictor1 = Predictor(model_path=args.model_path.format(args.use_model, args.feature_method), vocab_path=args.vocab_path, use_model=args.use_model,
                           decoder=args.decoder, alpha=args.alpha, beta=args.beta, lang_model_path=args.lang_model_path,
                           beam_size=args.beam_size, cutoff_prob=args.cutoff_prob, cutoff_top_n=args.cutoff_top_n,
                           use_gpu=args.use_gpu, use_pun=args.use_pun, pun_model_dir=args.pun_model_dir,
                           feature_method=args.feature_method)
    predictors.append(predictor1)

# 语音识别接口
@app.route("/recognition", methods=['POST'])
def recognition():
    f = request.files['audio']
    if f:
        # 临时保存路径
        file_path = os.path.join(args.save_path, f.filename)
        f.save(file_path)
        try:
            start = time.time()
            # 执行识别
            score, text = predictors[0].predict(audio_path=file_path, use_pun=args.use_pun, to_an=args.to_an)
            end = time.time()
            print("识别时间：%dms，识别结果：%s， 得分: %f" % (round((end - start) * 1000), text, score))
            result = str({"code": 0, "msg": "success", "result": text, "score": round(score, 3)}).replace("'", '"')
            return result
        except Exception as e:
            print(f'[{datetime.now()}] 短语音识别失败，错误信息：{e}', file=sys.stderr)
            return str({"error": 1, "msg": "audio read fail!"})
    return str({"error": 3, "msg": "audio is None!"})


# 长语音识别接口
@app.route("/recognition_long_audio", methods=['POST'])
def recognition_long_audio():
    f = request.files['audio']
    if f:
        # 临时保存路径
        file_path = os.path.join(args.save_path, f.filename)
        f.save(file_path)
        try:
            start = time.time()
            # 分割长音频
            audios_bytes = crop_audio_vad(file_path)
            texts = ''
            scores = []
            # 执行识别
            for i, audio_bytes in enumerate(audios_bytes):
                score, text = predictors[0].predict(audio_bytes=audio_bytes, use_pun=args.use_pun, to_an=args.to_an)
                texts = texts + text if args.use_pun else texts + '，' + text
                scores.append(score)
            end = time.time()
            print("识别时间：%dms，识别结果：%s， 得分: %f" % (round((end - start) * 1000), texts, sum(scores) / len(scores)))
            result = str({"code": 0, "msg": "success", "result": texts, "score": round(float(sum(scores) / len(scores)), 3)}).replace("'", '"')
            return result
        except Exception as e:
            print(f'[{datetime.now()}] 长语音识别失败，错误信息：{e}', file=sys.stderr)
            return str({"error": 1, "msg": "audio read fail!"})
    return str({"error": 3, "msg": "audio is None!"})


@app.route('/')
def home():
    return render_template("index.html")


# 流式识别WebSocket服务
async def stream_server_run(websocket, path):
    logger.info(f'有WebSocket连接建立：{websocket.remote_address}')
    use_predictor = None
    for predictor in predictors:
        if predictor.running: continue
        use_predictor = predictor
        use_predictor.running = True
        break
    if use_predictor is not None:
        frames = []
        while not websocket.closed:
            try:
                data = await websocket.recv()
                frames.append(data)
                if len(data) == 0: continue
                is_end = False
                # 判断是不是结束预测
                if b'end' == data[-3:]:
                    is_end = True
                    data = data[:-3]
                # 开始预测
                score, text = use_predictor.predict_stream(audio_bytes=data, use_pun=args.use_pun, to_an=args.to_an,
                                                           is_end=is_end)
                send_data = str({"code": 0, "result": text}).replace("'", '"')
                logger.info(f'向客户端发生消息：{send_data}')
                await websocket.send(send_data)
                # 结束了要关闭当前的连接
                if is_end: await websocket.close()
            except Exception as e:
                logger.error(f'识别发生错误：错误信息：{e}')
                try:
                    await websocket.send(str({"code": 2, "msg": "recognition fail!"}))
                except:pass
        # 重置流式识别
        use_predictor.reset_stream()
        use_predictor.running = False
        # 保存录音
        save_path = os.path.join(args.save_path, f"{int(time.time() * 1000)}.wav")
        audio_bytes = b''.join(frames)
        wf = wave.open(save_path, 'wb')
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(audio_bytes)
        wf.close()
    else:
        logger.error(f'语音识别失败，预测器不足')
        await websocket.send(str({"code": 1, "msg": "recognition fail, no resource!"}))
        websocket.close()


# 因为有多个服务需要使用线程启动
def start_server_thread():
    app.run(host=args.host, port=args.port)


if __name__ == '__main__':
    if not os.path.exists(args.save_path):
        os.makedirs(args.save_path)
    _thread.start_new_thread(start_server_thread, ())
    # 启动Flask服务
    server = websockets.serve(stream_server_run, args.host, args.port_stream)
    # 启动WebSocket服务
    asyncio.get_event_loop().run_until_complete(server)
    asyncio.get_event_loop().run_forever()
