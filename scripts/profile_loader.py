#!/usr/bin/env python3
"""
中央配置加载器 — 所有用户信息从此模块获取，禁止在各脚本中硬编码。

★ 设计原则:
  1. 唯一数据源: user_profile.json
  2. 所有脚本通过 from scripts.profile_loader import XXX 获取用户信息
  3. 本模块不依赖任何其他技能模块（无循环导入风险）
  4. WORK_DIR 通过环境变量 BOSS_WORK_DIR 或当前目录自动解析

用法:
    from scripts.profile_loader import (
        USER_NAME, CITY_CODE, TARGET_CITY, MIN_SALARY_K,
        DAILY_TARGET, SESSION_NAME, WORK_DIR, SKILL_DIR,
        SEARCH_KEYWORDS, USER_PROFILE, get_llm_prompt,
    )
"""

import os
import json
import shutil

# ═══════════════════════════════════════════════════════════
# 路径解析
# ═══════════════════════════════════════════════════════════

# 技能目录（自适应定位：脚本所在目录的父目录）
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 用户画像文件
USER_PROFILE_FILE = os.path.join(SKILL_DIR, "user_profile.json")

# 工作目录: 优先环境变量，否则使用当前目录
#   SKILL.md 指导用户 cd $workdir 后运行脚本，os.getcwd() 即为工作目录
#   多用户/多环境时可通过 BOSS_WORK_DIR 环境变量覆盖
WORK_DIR = os.environ.get("BOSS_WORK_DIR", os.getcwd())

# ═══════════════════════════════════════════════════════════
# 加载用户画像
# ═══════════════════════════════════════════════════════════


def _load_profile():
    """加载用户画像 JSON，返回 dict

    首次使用：user_profile.json 不存在但存在 user_profile.example.json 时，
    自动复制生成模板，再抛异常提示填写——不静默沿用示例城市（北京），避免投错城市。
    """
    if not os.path.exists(USER_PROFILE_FILE):
        example = os.path.join(SKILL_DIR, "user_profile.example.json")
        if os.path.exists(example):
            shutil.copyfile(example, USER_PROFILE_FILE)
            raise FileNotFoundError(
                f"首次使用：已从模板生成 {USER_PROFILE_FILE}\n"
                f"请至少填写 target_city / city_code / search_keywords / min_salary_k，然后重新运行。"
            )
        raise FileNotFoundError(
            f"用户画像文件不存在: {USER_PROFILE_FILE}\n"
            f"请复制 user_profile.example.json 为 user_profile.json 并按自身情况填写（见 SKILL.md）。"
        )
    with open(USER_PROFILE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# 模块级加载 — import 时即读取
USER_PROFILE = _load_profile()

# ═══════════════════════════════════════════════════════════
# 从用户画像提取的配置变量
# ═══════════════════════════════════════════════════════════

# --- 用户身份 ---
USER_NAME = USER_PROFILE.get("user_name", "")
ROLE_SUMMARY = USER_PROFILE.get("role_summary", "")
EXPERIENCE = USER_PROFILE.get("experience", "")
DOMAIN_DESCRIPTION = USER_PROFILE.get("domain_description", "")
DIRECTION_PRIORITY = USER_PROFILE.get("direction_priority", "")

# --- 求职目标 ---
TARGET_CITY = USER_PROFILE.get("target_city", "")
CITY_CODE = USER_PROFILE.get("city_code", "")
MIN_SALARY_K = USER_PROFILE.get("min_salary_k", 0)

# --- 投递参数 ---
DAILY_TARGET = USER_PROFILE.get("daily_target", 50)
SESSION_NAME = USER_PROFILE.get("session_name", "boss-main")

# --- 偏好 ---
PREFER = USER_PROFILE.get("prefer", [])
AVOID = USER_PROFILE.get("avoid", [])
PREFER_NO_BIG_COMPANY = USER_PROFILE.get("prefer_no_big_company", False)
PREFER_NO_HEADHUNTER = USER_PROFILE.get("prefer_no_headhunter", False)

# --- 搜索关键词（纯关键词，不含城市后缀）---
SEARCH_KEYWORDS = USER_PROFILE.get("search_keywords", [])

# --- 可选自定义排除列表 ---
C_EXCLUDE = USER_PROFILE.get("c_exclude", None)       # None = 不额外排除公司
T_EXCLUDE = USER_PROFILE.get("t_exclude", None)       # None = 不额外排除岗位词


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════


def get_profile():
    """获取完整用户画像（副本）"""
    return USER_PROFILE.copy()


def get_work_dir():
    """获取工作目录（运行时重新解析，支持环境变量动态切换）"""
    return os.environ.get("BOSS_WORK_DIR", os.getcwd())


def get_llm_prompt():
    """生成 LLM 审核提示词（基于用户画像，不硬编码任何用户信息）

    提示词中的「目标岗位范围」来自 user_profile.json 的 domain_description 字段；
    若未填写该字段则自动由 prefer/avoid 拼接。

    返回:
        str: 完整的 LLM 审核提示词
    """
    prefer_str = ", ".join(PREFER[:8]) if PREFER else "无"
    avoid_str = ", ".join(AVOID[:10]) if AVOID else "无"
    big_company_note = "不要大厂/上市公司（已上市、2000人以上）" if PREFER_NO_BIG_COMPANY else "无限制"
    headhunter_note = "不要猎头岗位" if PREFER_NO_HEADHUNTER else "无限制"

    # ★ 领域描述：优先使用 domain_description，否则由 prefer/avoid 自动拼接
    if DOMAIN_DESCRIPTION:
        domain_section = DOMAIN_DESCRIPTION
    else:
        prefer_lines = "目标领域（偏好岗位）：" + (", ".join(PREFER) if PREFER else "不限")
        avoid_lines = "排除领域：" + (", ".join(AVOID) if AVOID else "无特定排除")
        domain_section = prefer_lines + "\n" + avoid_lines

    direction_section = ("\n【方向权重】" + DIRECTION_PRIORITY) if DIRECTION_PRIORITY else ""

    return f"""你是一位严格的岗位匹配审核专家。请根据候选人画像和岗位信息，判断该岗位是否适合投递。

【候选人画像】
- 姓名：{USER_NAME}
- 经验：{EXPERIENCE}
- 目标城市：{TARGET_CITY}
- 最低薪资：月薪区间下界 {MIN_SALARY_K}K（判薪资看下限，不看上限；5-8K/5-9K 属不达标）
- 大厂偏好：{big_company_note}
- 猎头偏好：{headhunter_note}
- 偏重方向：{prefer_str}
- 排除方向：{avoid_str}

【目标岗位范围】
{domain_section}{direction_section}

【输入】
{{job_info}}

【输出格式】
必须严格按以下JSON格式输出，不要任何解释：
{{
  "decision": "APPROVE" | "REJECT" | "NEED_JD",
  "reason": "简短理由",
  "confidence": "high" | "medium" | "low"
}}

规则：
- APPROVE：岗位明显匹配目标领域，可直接投递
- REJECT：岗位明显不匹配或属于绝对不适合类别
- NEED_JD：从标题/行业无法明确判断，需要看JD详情再决定
- 如果不确定，宁可REJECT也不要勉强APPROVE
"""


# ═══════════════════════════════════════════════════════════
# 版本信息
# ═══════════════════════════════════════════════════════════

__version__ = "1.0.0"
__updated__ = "2026-08-04"
