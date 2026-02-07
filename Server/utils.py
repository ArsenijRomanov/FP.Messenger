import json
import time

def now_ts():
    return int(time.time())

def json_msg(obj):
    return json.dumps(obj, ensure_ascii=False)
