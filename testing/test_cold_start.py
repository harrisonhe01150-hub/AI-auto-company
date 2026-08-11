# -*- coding: utf-8 -*-
"""冷启动保护与去重：部署后不再从头重复应答"""
import base64, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wecom_core import WeComKfAdapter, CursorStore, WeComCrypto, OutboundReply

P = F = 0
def chk(n, c, e=""):
    global P, F
    P, F = (P+1, F) if c else (P, F+1)
    print(("PASS  " if c else "FAIL  ") + n + ("" if c else "   <- " + str(e)[:120]))

KEY = base64.b64encode(os.urandom(32)).decode().rstrip("=")
crypto = WeComCrypto("t", KEY, "corp")
NOW = time.time()

def mk(mid, age_sec, text="hi"):
    return {"msgid": mid, "open_kfid": "kfA", "external_userid": "u1", "origin": 3,
            "send_time": int(NOW - age_sec), "msgtype": "text", "text": {"content": text}}

class C:
    def __init__(self, msgs): self.msgs, self.page = msgs, 0
    def kf_sync_msg(self, cursor="", token="", limit=1000, open_kfid=""):
        out = self.msgs if self.page == 0 else []
        self.page += 1
        return {"msg_list": out, "next_cursor": "C1", "has_more": 0}
    def media_download(self, mid): return b"x"
    def kf_send_text(self, kf, u, t): return {}
    def kf_send_image(self, kf, u, m): return {}

def run(msgs, cursor_file, skip=300):
    got = []
    ad = WeComKfAdapter(on_message=lambda m: (got.append(m.text), OutboundReply(text="ok"))[1],
                        client=C(msgs), crypto=crypto,
                        cursor_store=CursorStore(cursor_file), cold_start_skip_seconds=skip)
    ad.sync_and_dispatch()
    return got, ad

for f in ("/tmp/cs1.json", "/tmp/cs2.json", "/tmp/cs3.json", "/tmp/cs4.json"):
    if os.path.exists(f): os.remove(f)

# 1) 冷启动：老消息只推游标不回复，新消息照回
got, ad = run([mk("m1", 7200, "两小时前"), mk("m2", 3600, "一小时前"), mk("m3", 10, "刚发的")], "/tmp/cs1.json")
chk("冷启动跳过历史消息", got == ["刚发的"], got)
chk("跳过计数正确", ad.stats["skipped_stale"] == 2, ad.stats)
chk("游标已推进", CursorStore("/tmp/cs1.json").get() == "C1")

# 2) 非冷启动（游标已存在）：全部正常应答
CursorStore("/tmp/cs2.json").set("OLD")
got, ad = run([mk("n1", 7200, "老消息"), mk("n2", 5, "新消息")], "/tmp/cs2.json")
chk("热启动不跳过", got == ["老消息", "新消息"], got)

# 3) msgid 去重
ad2 = WeComKfAdapter(on_message=lambda m: OutboundReply(text="ok"),
                     client=C([mk("d1", 5), mk("d1", 5), mk("d2", 5)]), crypto=crypto,
                     cursor_store=CursorStore("/tmp/cs3.json"), cold_start_skip_seconds=0)
n = ad2.sync_and_dispatch()
chk("同批重复msgid只处理一次", n == 2 and ad2.stats["skipped_dup"] == 1, ad2.stats)

# 4) 关闭保护
got, ad = run([mk("z1", 99999, "远古消息")], "/tmp/cs4.json", skip=0)
chk("skip=0 时不跳过", got == ["远古消息"], got)

print(f"\n结果: {P} passed, {F} failed")
sys.exit(1 if F else 0)
