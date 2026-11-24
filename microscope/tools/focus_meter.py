#!/usr/bin/env python3
"""手动拧焦验证（开发方案 2.3）：每 0.6 秒向显微服务要一次清晰度并画成条形，边拧焦边看数值先升后降。

在电脑上运行（需能连 MaixCAM2 的 SIoT）：
  export SIOT_PASSWORD=...      # 与 MaixCAM2 /etc/siot/monitor.json 一致
  python3 tools/focus_meter.py --seconds 60 --csv 20261111_手动拧焦_01.csv
慢慢从模糊拧到清晰再拧过头，记录下来的分数应先升后降，峰值处与人眼看到最清晰的位置一致。
"""
import argparse, csv, json, os, sys, threading, time


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--server", default="192.168.50.118")
    p.add_argument("--user", default=os.environ.get("SIOT_USER", ""))
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--csv")
    a = p.parse_args()
    import paho.mqtt.client as mqtt
    c = mqtt.Client(client_id="focus_meter_%d" % int(time.time()))
    c.username_pw_set(a.user, os.environ.get("SIOT_PASSWORD") or sys.exit("请先设置 SIOT_PASSWORD"))
    got, cv = {}, threading.Condition()

    def on_msg(cl, u, m):
        d = json.loads(m.payload)
        with cv:
            got[d.get("seq")] = d
            cv.notify_all()
    c.on_message = on_msg
    c.on_connect = lambda cl, u, f, rc: cl.subscribe("siot/scope_focus")
    c.connect(a.server, 1883, 10)
    c.loop_start()
    time.sleep(0.5)
    rows, top, seq, t0 = [], 1.0, 0, time.time()
    while time.time() - t0 < a.seconds:
        seq += 1
        c.publish("siot/scope_focus_req", json.dumps({"seq": seq, "z": None}))
        with cv:
            end = time.time() + 8
            while seq not in got and time.time() < end:
                cv.wait(0.2)
        d = got.get(seq)
        if not d or "score" not in d:
            print("%5.1f s  无回复 %s" % (time.time() - t0, d and d.get("error")))
            continue
        top = max(top, d["score"])
        bar = "#" * int(50 * d["score"] / top)
        print("%5.1f s  %10.1f  %s" % (time.time() - t0, d["score"], bar), flush=True)
        rows.append((round(time.time() - t0, 2), d["score"], d.get("ms")))
        time.sleep(0.6)
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["t_s", "score", "server_ms"]); w.writerows(rows)
        print("已保存", a.csv)


if __name__ == "__main__":
    main()
