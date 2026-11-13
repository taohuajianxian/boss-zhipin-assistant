#!/usr/bin/env python3
"""
模块 G: 投递汇报 HTML 页生成

职责:
  读取 stream_progress.json 投递进度文件，
  生成自包含的 HTML 汇报页（有本地二维码时内嵌 Base64，缺省只显示微信号，无外部依赖）。

独立可运行:
    python report_builder.py --progress stream_progress.json --output 投递汇总.html
"""

import base64
import json
import os
import sys
from datetime import datetime

# SKILL 根目录（从自身路径推导：scripts/report_builder.py → 上级即 SKILL_DIR）
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(SKILL_DIR, "assets")
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)


def _entitlement_info():
    """读取授权状态；独立跑 CLI 时也能用，失败则降级为不显示该区块。"""
    try:
        from scripts.entitlement import status_snapshot
        return status_snapshot()
    except Exception:
        return None


def _esc(text):
    """HTML 转义"""
    if not text:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _load_qr_base64(filename):
    """从 assets 目录读取二维码图片，返回 data:image 格式的 base64 字符串。

    二维码属于可选的本地资源，不随发布包分发，缺失属正常情况：
    直接返回空字符串，由调用方降级为「只显示微信号」，不打印警告。
    """
    filepath = os.path.join(ASSETS_DIR, filename)
    if not os.path.exists(filepath):
        return ""
    try:
        with open(filepath, "rb") as f:
            raw = f.read()
        ext = os.path.splitext(filename)[1].lstrip(".").lower()
        mime_map = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}
        mime = mime_map.get(ext, "jpeg")
        return f"data:image/{mime};base64,{base64.b64encode(raw).decode()}"
    except Exception:
        return ""


def _wechat_qr():
    """返回微信二维码的 data URI。

    取值顺序：
      1. 本机 assets/wechat_qr.jpg（存在则用，方便随时换码）；
      2. 随包的 scripts/qr_data.py 内嵌 base64（发布平台禁二进制，包里不带图片文件）。
    都取不到时返回空字符串，页面降级为只显示微信号。
    """
    local = _load_qr_base64("wechat_qr.jpg")
    if local:
        return local
    for importer in ("relative", "flat"):
        try:
            if importer == "relative":
                from .qr_data import WECHAT_QR_DATA_URI
            else:
                from qr_data import WECHAT_QR_DATA_URI
            return WECHAT_QR_DATA_URI
        except Exception:
            continue
    return ""


def build_report(progress_file="stream_progress.json", output_path="投递汇总.html",
                 entitlement_info=None):
    """生成投递汇报 HTML 页

    参数:
        progress_file: 投递进度 JSON 文件路径
        output_path: 输出 HTML 文件路径
        entitlement_info: 授权状态 dict（由 serial_loop 传入，避免重复计算）；
                          不传则本模块自行读取。

    返回:
        output_path (str) 或 None (失败时)
    """
    if not os.path.exists(progress_file):
        print(f"进度文件不存在: {progress_file}")
        return None

    with open(progress_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    delivered = data.get('delivered', [])
    failed = data.get('failed', [])

    total_delivered = len(delivered)
    total_failed = len(failed)

    # 按行业分组
    industry_groups = {}
    for job in delivered:
        industry = job.get('industry', '未知') or '未知'
        if industry not in industry_groups:
            industry_groups[industry] = []
        industry_groups[industry].append(job)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>投递汇报 - {now}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, "Noto Sans CJK SC", "Microsoft YaHei", sans-serif; background: #f5f5f5; color: #333; line-height: 1.6; }}
.container {{ max-width: 900px; margin: 0 auto; padding: 20px; }}
.header {{ background: linear-gradient(135deg, #00b38a, #009688); color: #fff; padding: 30px; border-radius: 12px; text-align: center; margin-bottom: 20px; }}
.header h1 {{ font-size: 28px; margin-bottom: 8px; }}
.header .subtitle {{ opacity: 0.9; font-size: 14px; }}
.stats {{ display: flex; gap: 16px; margin-bottom: 20px; }}
.stat-card {{ flex: 1; background: #fff; padding: 20px; border-radius: 8px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.stat-card .num {{ font-size: 32px; font-weight: bold; color: #00b38a; }}
.stat-card .label {{ font-size: 13px; color: #888; margin-top: 4px; }}
.stat-card.fail .num {{ color: #e74c3c; }}
.section {{ background: #fff; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.section h2 {{ font-size: 18px; margin-bottom: 16px; padding-bottom: 10px; border-bottom: 2px solid #f0f0f0; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th {{ text-align: left; padding: 10px 8px; color: #888; font-weight: 500; border-bottom: 1px solid #eee; }}
td {{ padding: 10px 8px; border-bottom: 1px solid #f8f8f8; }}
tr:hover td {{ background: #fafafa; }}
.tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; background: #e8f5e9; color: #2e7d32; }}
.tag.fail {{ background: #fbe9e7; color: #c62828; }}
.tip-section {{ background: #fff; border-radius: 8px; padding: 30px; text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 20px; }}
.tip-section h2 {{ font-size: 20px; margin-bottom: 12px; }}
.tip-section p {{ color: #888; margin-bottom: 20px; }}
.tip-codes {{ display: flex; justify-content: center; gap: 40px; flex-wrap: wrap; }}
.tip-code .placeholder {{ width: 180px; height: 180px; border: 2px dashed #ddd; border-radius: 8px; display: flex; align-items: center; justify-content: center; color: #aaa; font-size: 13px; }}
.tip-code .name {{ margin-top: 8px; font-size: 14px; color: #555; }}
.empty {{ text-align: center; padding: 40px; color: #aaa; }}
.status-line {{ display: flex; gap: 12px; padding: 7px 0; border-bottom: 1px solid #f6f6f6; font-size: 14px; }}
.status-line .k {{ width: 74px; color: #888; flex-shrink: 0; }}
.status-line .v {{ color: #333; }}
.status-note {{ margin-top: 12px; font-size: 13px; color: #888; }}
.tip-section .soft {{ color: #888; font-size: 13px; margin: 16px 0 0; }}
.wechat-id {{ margin-top: 14px; font-size: 15px; color: #009688; font-weight: 500; }}
.activate-box {{ background: #f8faf9; border: 1px solid #e3efeb; border-radius: 8px;
                 padding: 18px; margin-top: 24px; text-align: left; }}
.activate-box .t {{ font-size: 14px; font-weight: 500; margin-bottom: 6px; }}
.activate-box p {{ font-size: 13px; color: #777; margin: 0 0 12px; }}
.activate-row {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.activate-row input {{ flex: 1; min-width: 200px; padding: 8px 10px; border: 1px solid #d8e2df;
                       border-radius: 6px; font-size: 14px; }}
.activate-row button {{ padding: 8px 16px; border: 0; border-radius: 6px; background: #00b38a;
                        color: #fff; cursor: pointer; font-size: 14px; }}
.activate-row button:hover {{ background: #009e7a; }}
.activate-box .tiny {{ font-size: 12px; color: #aaa; margin: 10px 0 0; }}
.copy-msg {{ font-size: 13px; color: #009688; min-height: 18px; margin-top: 8px; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>投递汇报</h1>
    <div class="subtitle">生成时间: {now}</div>
  </div>
  <div class="stats">
    <div class="stat-card"><div class="num">{total_delivered}</div><div class="label">成功投递</div></div>
    <div class="stat-card fail"><div class="num">{total_failed}</div><div class="label">投递失败</div></div>
    <div class="stat-card"><div class="num">{len(industry_groups)}</div><div class="label">覆盖行业</div></div>
  </div>
  <div class="section">
    <h2>投递明细</h2>
"""

    if delivered:
        html += """    <table>
      <thead><tr><th>#</th><th>岗位名称</th><th>公司</th><th>薪资</th><th>行业</th><th>结果</th></tr></thead>
      <tbody>
"""
        for i, job in enumerate(delivered, 1):
            html += (f'      <tr><td>{i}</td><td>{_esc(job.get("title",""))}</td>'
                     f'<td>{_esc(job.get("company",""))}</td>'
                     f'<td>{_esc(job.get("salary",""))}</td>'
                     f'<td>{_esc(job.get("industry",""))}</td>'
                     f'<td><span class="tag">成功</span></td></tr>\n')
        html += "    </tbody>\n  </table>\n"
    else:
        html += '    <div class="empty">暂无投递记录</div>\n'

    html += "  </div>\n"

    if failed:
        html += """  <div class="section">
    <h2>失败记录</h2>
    <table>
      <thead><tr><th>#</th><th>岗位名称</th><th>公司</th><th>失败原因</th></tr></thead>
      <tbody>
"""
        for i, job in enumerate(failed, 1):
            html += (f'      <tr><td>{i}</td><td>{_esc(job.get("title",""))}</td>'
                     f'<td>{_esc(job.get("company",""))}</td>'
                     f'<td><span class="tag fail">{_esc(job.get("reason",""))}</span></td></tr>\n')
        html += "    </tbody>\n  </table>\n  </div>\n"

    # 授权状态（由 serial_loop 传入，或本模块自行读取）
    snap = entitlement_info if entitlement_info is not None else _entitlement_info()
    if snap:
        src_label = {"cloud": "云端同步版", "bundle": "随包基础版",
                     "none": "未加载"}.get(snap.get("plan_source"), "—")
        html += f"""  <div class="section status-box">
    <h2>使用状态</h2>
    <div class="status-line"><span class="k">授权</span><span class="v">{_esc(snap.get('title'))}</span></div>
    <div class="status-line"><span class="k">指令包</span><span class="v">{_esc(snap.get('plan_version') or '未加载')}（{src_label}）</span></div>
    <div class="status-line"><span class="k">今天</span><span class="v">已投 {snap.get('free_used_today', 0)} / {snap.get('free_limit', 150)} 份</span></div>
    <div class="status-note">{_esc(snap.get('detail'))}</div>
  </div>
"""

    # 只展示微信：本机有 assets 图则用图，否则用随包内嵌 base64（发布包不含图片文件）
    wechat_qr = _wechat_qr()
    wechat_id = _esc((snap or {}).get("wechat_id") or "")

    html += """  <div class="tip-section">
    <h2>继续用 / 续期</h2>
    <p>加我微信拿激活码。页面改版后新版指令包我这边会一直同步；用着有问题也可以直接问我。</p>"""

    # 二维码属于可选本地资源，不随包分发（发布平台禁止二进制文件）；
    # 有本地图片时内嵌展示，没有时只展示微信号，保持页面干净。
    if wechat_qr:
        html += f"""    <div class="tip-codes">
      <div class="tip-code"><img src="{wechat_qr}" style="width:auto;height:auto;max-width:220px;max-height:300px;border-radius:8px;object-fit:contain;" alt="微信二维码"><div class="name">微信</div></div>
    </div>"""

    if wechat_id:
        html += f'    <div class="wechat-id">微信号：{wechat_id}</div>\n'

    html += """    <p class="soft">手头不方便的话直接说一声，我送你一张，别有压力。</p>
    <div class="activate-box">
      <div class="t">拿到激活码之后</div>
      <p>直接把码发给你的 Agent 就好，它会跑激活脚本。也可以在下面粘一次，复制一句现成的话：</p>
      <div class="activate-row">
        <input id="code-input" type="text" placeholder="粘贴激活码，例如 ABCD-1234-EFGH">
        <button onclick="copyActivate()">复制激活指令</button>
      </div>
      <div class="copy-msg" id="copy-msg"></div>
      <p class="tiny">这一页是静态文件，这里只帮你拼一句话，不会真的激活；真正的激活由 Agent 执行 scripts/entitlement.py 完成。</p>
    </div>
  </div>
</div>
<script>
function copyActivate(){
  var c = (document.getElementById('code-input').value || '').trim();
  var msg = document.getElementById('copy-msg');
  if(!c){ msg.textContent = '先粘贴激活码'; return; }
  var text = '请帮我激活这个激活码：' + c;
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(
      function(){ msg.textContent = '已复制，粘回对话发给 Agent 就行'; },
      function(){ msg.textContent = '复制失败，请手动复制：' + text; }
    );
  } else { msg.textContent = '请手动复制：' + text; }
}
</script>
</body>
</html>"""

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"投递汇报已生成: {output_path}")
    print(f"  成功投递: {total_delivered} 份")
    print(f"  投递失败: {total_failed} 份")
    print(f"  覆盖行业: {len(industry_groups)} 个")

    return output_path


def main():
    import argparse
    parser = argparse.ArgumentParser(description="投递汇报 HTML 生成")
    parser.add_argument("--progress", default="stream_progress.json", help="进度文件路径")
    parser.add_argument("--output", default="投递汇总.html", help="输出 HTML 路径")
    args = parser.parse_args()

    result = build_report(args.progress, args.output)
    if not result:
        sys.exit(1)


if __name__ == "__main__":
    main()
