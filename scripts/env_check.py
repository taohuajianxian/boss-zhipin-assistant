#!/usr/bin/env python3
"""
模块 A: 环境自检与修复 (v2 — 增加账户匹配引导)

★ v2 新增:
  - check_account_match() — 检测本地用户信息与登录账户是否匹配
  - print_account_guidance() — 账户不匹配时的引导流程
  - 集成到 check_environment() 和 main() 中

职责:
  1. 检查 WebBridge daemon 是否可达 (端口 10086)
  2. 检查 Chrome 标签页是否可交互
  3. 检查 BOSS直聘 登录状态
  4. ★ 检查本地用户信息与登录账户是否匹配（用户名 + 城市）
  5. 自动修复: 清理 PID 残留 → 重试
  6. 修复失败则输出修复指引

独立可运行:
    python env_check.py

也可被其他模块 import:
    from scripts.env_check import check_environment, auto_repair, check_account_match
    ok, issues = check_environment()
    if not ok:
        auto_repair()
"""

import sys
import os
import time
import json

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts.webbridge_client import (
    SESSION, health_check, ensure_active_tab,
    clean_pid_files, evaluate, navigate,
)
from scripts.profile_loader import (
    USER_NAME, TARGET_CITY, CITY_CODE, USER_PROFILE_FILE,
)


# ═══════════════════════════════════════════════════════════
# 基础环境检查
# ═══════════════════════════════════════════════════════════

def check_webbridge():
    """检查 WebBridge daemon 是否可达"""
    return health_check()


def check_chrome_tab():
    """检查 Chrome 标签页是否可交互"""
    try:
        result = evaluate("1+1")
        return str(result) == "2"
    except Exception:
        return False


def check_boss_login():
    """检查 BOSS直聘 登录状态"""
    try:
        current_url = evaluate("window.location.href") or ""
        if "zhipin.com" not in current_url:
            navigate("https://www.zhipin.com/", wait_range=(5, 7))

        login_status = evaluate("""
        (function(){
            var userEl = document.querySelector('.user-info, .nav-user, .label-text');
            if (userEl && userEl.offsetParent !== null) {
                var text = userEl.textContent.trim();
                if (text && text !== '登录' && text !== '注册') {
                    return 'logged_in:' + text.substring(0, 20);
                }
            }
            var loginBtn = document.querySelector('a[href*="login"], .btn-login');
            if (loginBtn && loginBtn.offsetParent !== null) {
                return 'not_logged_in';
            }
            return 'unknown';
        })()
        """) or "unknown"

        return login_status
    except Exception as e:
        return f"error:{e}"


# ═══════════════════════════════════════════════════════════
# ★ v2 新增: 账户匹配检查
# ═══════════════════════════════════════════════════════════

def _get_logged_in_user_name():
    """从 BOSS直聘 页面提取已登录用户的显示名

    尝试多个 DOM 选择器，返回非空的用户名文本。

    返回:
        str: 登录用户名（可能为空字符串）
    """
    result = evaluate("""
    (function(){
        // ★ 必须用精确选择器 .nav-figure .label-text 避免被岗位列表中的 .user-info 误匹配
        // .user-info 在推荐页会匹配99个岗位卡片中的公司名，禁止使用
        var selectors = [
            '.nav-figure .label-text',
            '.nav-figure',
            '.nav-user .label-text',
            '.nav-user',
            '.user-name',
            '.label-text-name',
            '.header-user .name',
            '.user-avatar-text'
        ];
        for (var i = 0; i < selectors.length; i++) {
            var el = document.querySelector(selectors[i]);
            if (el && el.offsetParent !== null) {
                var text = el.textContent.trim();
                if (text && text !== '登录' && text !== '注册' && text.length <= 20) {
                    return text;
                }
            }
        }

        // 方式2: 从 body 文本中提取 "你好，XXX" 模式
        var bodyText = document.body.innerText || '';
        var patterns = [
            /你好[，,\\s]+([^\\n]{2,10})/,
            /欢迎[，,\\s]+([^\\n]{2,10})/,
        ];
        for (var j = 0; j < patterns.length; j++) {
            var match = bodyText.match(patterns[j]);
            if (match) return match[1].trim();
        }

        return '';
    })()
    """) or ""
    return result.strip()


def _get_logged_in_city():
    """从 BOSS直聘 页面提取当前选择的城市

    从 URL 参数或页面城市选择器中提取城市信息。

    返回:
        dict: {
            "city_code": str,   # URL 中的城市编码（如 "101210100"）
            "city_name": str,   # 页面上显示的城市名（如 "北京"）
        }
    """
    result = evaluate("""
    (function(){
        // 方式1: 从 URL 提取城市编码
        var url = window.location.href;
        var codeMatch = url.match(/city=(\\d+)/);
        var cityCode = codeMatch ? codeMatch[1] : '';

        // 方式2: 从页面城市选择器提取城市名
        var cityName = '';
        var citySelectors = [
            '.city-sel .current',
            '.current-city',
            '.city-name',
            '.job-city .selected',
            '.filter-city .active',
            '.city-selector .current'
        ];
        for (var i = 0; i < citySelectors.length; i++) {
            var el = document.querySelector(citySelectors[i]);
            if (el && el.offsetParent !== null) {
                var text = el.textContent.trim();
                if (text && text.length <= 10) {
                    cityName = text;
                    break;
                }
            }
        }

        return JSON.stringify({city_code: cityCode, city_name: cityName});
    })()
    """) or "{}"

    try:
        data = json.loads(result)
        return {
            "city_code": data.get("city_code", ""),
            "city_name": data.get("city_name", ""),
        }
    except (json.JSONDecodeError, TypeError):
        return {"city_code": "", "city_name": ""}


def check_account_match():
    """★ 检查本地用户信息是否与登录账户匹配

    读取 user_profile.json 中的 user_name 和 target_city/city_code，
    与当前登录的 BOSS直聘 账户信息进行比对。

    检查项:
      1. 用户名匹配: 本地 user_profile.json 的 user_name 是否出现在登录账户名中
      2. 城市匹配: URL 中的 city 参数是否与 user_profile.json 的 city_code 一致

    返回:
        dict: {
            "matched": bool,           # 是否完全匹配
            "user_name_match": bool,   # 用户名是否匹配
            "city_match": bool,        # 城市是否匹配
            "logged_in_name": str,     # 登录账户名
            "expected_name": str,      # 本地配置的用户名
            "logged_in_city_code": str,# 登录页面的城市编码
            "logged_in_city_name": str,# 登录页面的城市名
            "expected_city": str,      # 本地配置的目标城市
            "expected_city_code": str, # 本地配置的城市编码
            "issues": list[str],       # 不匹配的问题列表
        }
    """
    issues = []

    # 1. 读取本地用户配置
    expected_name = USER_NAME
    expected_city = TARGET_CITY
    expected_city_code = str(CITY_CODE)

    # 2. 获取登录账户信息
    logged_in_name = _get_logged_in_user_name()
    city_info = _get_logged_in_city()
    logged_in_city_code = city_info["city_code"]
    logged_in_city_name = city_info["city_name"]

    # 3. 用户名匹配检查
    user_name_match = True
    if expected_name and logged_in_name:
        # 模糊匹配: 本地用户名出现在登录名中，或登录名出现在本地用户名中
        if expected_name in logged_in_name or logged_in_name in expected_name:
            user_name_match = True
        else:
            user_name_match = False
            issues.append(
                f"用户名不匹配: 本地配置 '{expected_name}'，"
                f"登录账户 '{logged_in_name}'"
            )
    elif expected_name and not logged_in_name:
        # 无法获取登录用户名，不算不匹配，但提示
        issues.append(f"无法从页面获取登录用户名（本地配置: '{expected_name}'）")
    # 如果本地没配 user_name，跳过此检查

    # 4. 城市匹配检查
    city_match = True
    if logged_in_city_code and expected_city_code:
        if logged_in_city_code != expected_city_code:
            city_match = False
            city_display = logged_in_city_name or logged_in_city_code
            issues.append(
                f"城市不匹配: 本地配置 '{expected_city}'({expected_city_code})，"
                f"页面当前 '{city_display}'({logged_in_city_code})"
            )
    elif not logged_in_city_code:
        # URL 中无城市参数（可能在首页），跳过此检查
        pass

    # 5. 综合判断
    matched = user_name_match and city_match

    return {
        "matched": matched,
        "user_name_match": user_name_match,
        "city_match": city_match,
        "logged_in_name": logged_in_name,
        "expected_name": expected_name,
        "logged_in_city_code": logged_in_city_code,
        "logged_in_city_name": logged_in_city_name,
        "expected_city": expected_city,
        "expected_city_code": expected_city_code,
        "issues": issues,
    }


def print_account_guidance(match_result):
    """输出账户不匹配的引导信息

    参数:
        match_result: check_account_match() 的返回值
    """
    print("\n" + "=" * 60)
    print("★ 账户匹配检查")
    print("=" * 60)

    # ★ 20261113: user_name 为空时（模板未填写）不能报"匹配"，否则首用用户会被误导
    prof_unset = not str(match_result['expected_name'] or '').strip()
    name_show = match_result['expected_name'] or '(未填写)'
    if match_result['expected_city']:
        city_show = f"{match_result['expected_city']} (编码: {match_result['expected_city_code']})"
    else:
        city_show = '(未填写)'

    print(f"\n  本地配置 (user_profile.json):")
    print(f"    用户名: {name_show}")
    print(f"    目标城市: {city_show}")
    if prof_unset:
        print(f"    ℹ 模板尚未填写：用户名校验不生效，请按自身情况补全")

    print(f"\n  登录账户 (BOSS直聘页面):")
    print(f"    用户名: {match_result['logged_in_name'] or '(未获取到)'}")
    if match_result['logged_in_city_name']:
        print(f"    当前城市: {match_result['logged_in_city_name']} (编码: {match_result['logged_in_city_code']})")
    elif match_result['logged_in_city_code']:
        print(f"    当前城市编码: {match_result['logged_in_city_code']}")
    else:
        print(f"    当前城市: (未获取到，可能在首页)")

    if match_result["matched"]:
        if prof_unset:
            print(f"\n  ⚠ 未填写 user_name，用户名校验已跳过（状态是“未配置”，不是“匹配”）")
            print(f"    → 首次使用请编辑: {USER_PROFILE_FILE}")
        else:
            print(f"\n  ★ 本地用户信息与登录账户匹配")
    else:
        print(f"\n  ⚠ 检测到 {len(match_result['issues'])} 个不匹配项:")
        for issue in match_result["issues"]:
            print(f"    - {issue}")

        print(f"\n  修复指引:")
        for issue in match_result["issues"]:
            if "用户名不匹配" in issue:
                print(f"    [用户名] 请确认当前登录的 BOSS直聘 账户是否正确:")
                print(f"      1. 如果登录了错误的账户，请在 Chrome 中退出并重新登录")
                print(f"      2. 如果需要更新本地配置，请修改 user_profile.json 中的 user_name 字段")
                print(f"      3. 修改路径: {USER_PROFILE_FILE}")
            elif "城市不匹配" in issue:
                print(f"    [城市] 请确认目标城市是否正确:")
                print(f"      1. 如果需要切换城市，请在 BOSS直聘 页面顶部选择目标城市")
                print(f"      2. 如果需要更新本地配置，请修改 user_profile.json 中的:")
                print(f"         - target_city: 城市名（如 '{TARGET_CITY}'）")
                print(f"         - city_code: 城市编码（如 '{CITY_CODE}'）")
                print(f"      3. 城市编码查询: https://www.zhipin.com/ 切换城市后查看 URL 中的 city 参数")
            elif "无法从页面获取登录用户名" in issue:
                print(f"    [用户名] 无法自动获取登录用户名:")
                print(f"      1. 请手动确认当前登录的账户是否正确")
                print(f"      2. 如果页面未完全渲染，等待几秒后重试环境检查")

    print("\n" + "=" * 60)


# ═══════════════════════════════════════════════════════════
# 完整环境检查
# ═══════════════════════════════════════════════════════════

def check_environment():
    """完整环境检查

    返回:
        (ok: bool, issues: list[str])
    """
    issues = []

    if not check_webbridge():
        issues.append("WebBridge daemon 不可达 (端口 10086)")
        return False, issues

    if not check_chrome_tab():
        issues.append("Chrome 标签页不可交互 (可能浏览器未启动或 WebBridge 未连接)")
        return False, issues

    login = check_boss_login()
    if login == "not_logged_in":
        issues.append("BOSS直聘 未登录")
        return False, issues
    elif login.startswith("error"):
        issues.append(f"登录状态检测异常: {login}")
        return False, issues
    elif login == "unknown":
        issues.append("登录状态未知 (页面可能未完全渲染)")
        return False, issues

    # ★ v2 新增: 账户匹配检查（登录成功后才检查）
    match_result = check_account_match()
    if not match_result["matched"]:
        for issue in match_result["issues"]:
            issues.append(issue)

    return len(issues) == 0, issues


def auto_repair():
    """自动修复: 清理 PID → 等待 → 重试

    返回:
        repaired: bool
    """
    print("[修复] 清理 PID 残留文件...")
    cleaned = clean_pid_files()
    if cleaned:
        print(f"[修复] 已清理: {cleaned}")

    print("[修复] 等待 3 秒后重试...")
    time.sleep(3)

    if not check_webbridge():
        print("[修复] WebBridge 仍不可达")
        return False

    print("[修复] 尝试重新建立标签页...")
    if ensure_active_tab():
        print("[修复] 标签页已恢复")
        return True

    return False


def print_repair_guidance(issues):
    """输出修复指引"""
    print("\n" + "=" * 60)
    print("环境检查未通过，请按以下步骤修复:")
    print("=" * 60)

    for i, issue in enumerate(issues, 1):
        print(f"\n  问题 {i}: {issue}")

        if "WebBridge" in issue:
            print("  修复步骤:")
            print("    1. 确认 Chrome 浏览器已启动")
            print("    2. 确认 Kimi WebBridge 插件已安装并启用")
            print("    3. 手动启动 daemon: kimi-webbridge start")
            print('    4. 验证: python -c "from scripts.webbridge_client import health_check; print(health_check())"')
        elif "标签页" in issue:
            print("  修复步骤:")
            print("    1. 在 Chrome 中手动打开一个标签页")
            print("    2. 确认 WebBridge 插件图标显示已连接")
            print("    3. 重试环境检查")
        elif "未登录" in issue:
            print("  修复步骤:")
            print("    1. 在 Chrome 中打开 https://www.zhipin.com/")
            print("    2. 手动登录 BOSS直聘 账号")
            print("    3. 登录成功后重试")
        elif "用户名不匹配" in issue:
            print("  修复步骤:")
            print("    1. 确认当前登录的 BOSS直聘 账户是否正确")
            print("    2. 如需更新本地配置，修改 user_profile.json 中的 user_name")
        elif "城市不匹配" in issue:
            print("  修复步骤:")
            print("    1. 在 BOSS直聘 页面顶部切换到目标城市")
            print("    2. 如需更新本地配置，修改 user_profile.json 中的 target_city 和 city_code")

    print("\n" + "=" * 60)
    print("修复完成后请回复 '环境配置完成'")
    print("=" * 60)


def main():
    """CLI 入口: 环境自检（含账户匹配）"""
    print("=" * 60)
    print("环境自检 (模块 A v2 — 含账户匹配)")
    print("=" * 60)

    ok, issues = check_environment()

    if not ok:
        print(f"\n检测到 {len(issues)} 个问题:")
        for issue in issues:
            print(f"  - {issue}")

        # 区分基础环境问题和账户匹配问题
        basic_issues = [i for i in issues if "不匹配" not in i and "无法" not in i]
        account_issues = [i for i in issues if "不匹配" in i or "无法" in i]

        if basic_issues:
            print("\n尝试自动修复基础环境问题...")
            if auto_repair():
                print("\n自动修复成功! 重新检查...")
                ok, issues = check_environment()
                # 重新分类
                account_issues = [i for i in issues if "不匹配" in i or "无法" in i]

    # 即使基础环境OK，也显示账户匹配结果
    print("\n" + "=" * 60)
    if ok:
        print("环境检查通过，可以开始投递")
        # 显示账户匹配详情
        match_result = check_account_match()
        print_account_guidance(match_result)
    else:
        # 分开显示基础环境问题和账户匹配问题
        basic_issues = [i for i in issues if "不匹配" not in i and "无法" not in i]
        account_issues = [i for i in issues if "不匹配" in i or "无法" in i]

        if basic_issues:
            print("环境检查未通过")
            print_repair_guidance(basic_issues)
        elif account_issues:
            # 基础环境OK，但有账户匹配问题
            print("基础环境检查通过，但检测到账户匹配问题:")
            match_result = check_account_match()
            print_account_guidance(match_result)
            print("\n★ 账户不匹配不阻止投递，但建议先修复以确保投递到正确的城市。")
            print("  确认无误后可回复 '继续投递'")

    # 退出码: 基础环境问题=1，仅账户匹配问题=0
    has_basic_issues = any("不匹配" not in i and "无法" not in i for i in issues)
    if has_basic_issues:
        sys.exit(1)


if __name__ == "__main__":
    main()
