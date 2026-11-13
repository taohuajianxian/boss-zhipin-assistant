#!/usr/bin/env python3
"""
模块 D: 批量读取左侧表面信息 → 缓存文件

职责:
  在推荐页懒加载穷尽后，一次性提取所有岗位卡片的表面信息，
  保存至缓存文件。

★ v4.4 精简抓取:
  只保留7个核心字段（page_order, jobId, title, salary, salary_max_k,
  company, industry），去掉了 city(用户配置城市,通过推荐页已限定)、stage、online、
  welfare(单条最长80字)、degree、experience 等6个对AI初筛无价值的字段，
  数据量减少约40%。

★ 顺序约束:
  每条岗位记录包含 page_order 字段（0=页面最顶部），
  AI 筛选生成 jd_to_read.json 时必须保持此顺序，禁止重排。

独立可运行:
    python surface_cache.py --output surface_cache.json

也可被其他模块 import:
    from scripts.surface_cache import extract_all_surface, save_cache, load_cache
    jobs = extract_all_surface()
    save_cache(jobs, "surface_cache.json")
"""

import json
import os
import sys
import time

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts.webbridge_client import (
    SESSION, evaluate, extract_jobs, decrypt_salary, parse_salary_max,
)

# ★ v4.4 精简字段白名单 — 只保留AI初筛需要的字段
KEEP_FIELDS = ['page_order', 'jobId', 'title', 'salary', 'salary_max_k', 'company', 'industry']


def extract_all_surface():
    """从当前推荐页提取所有岗位卡片的表面信息（精简版）

    ★ v4.4 只保留7个核心字段:
      page_order, jobId, title, salary, salary_max_k, company, industry

    返回:
        list[dict]: 岗位列表
    """
    print("[缓存] 开始批量提取岗位表面信息...")

    # 使用 webbridge_client 的 extract_jobs 提取（内部已解密薪资）
    jobs = extract_jobs()

    # 补充计算字段
    for job in jobs:
        salary_str = job.get('salary', '')
        job['salary_max_k'] = parse_salary_max(salary_str)

    # 去重（按 jobId）
    seen = set()
    unique_jobs = []
    for job in jobs:
        jid = job.get('jobId', '')
        if jid and jid not in seen:
            seen.add(jid)
            unique_jobs.append(job)

    # ★ 添加 page_order 字段
    for i, job in enumerate(unique_jobs):
        job['page_order'] = i

    # ★ v4.4 精简字段 — 只保留白名单中的字段
    slim_jobs = []
    for job in unique_jobs:
        slim = {k: job.get(k, '') for k in KEEP_FIELDS}
        slim_jobs.append(slim)

    print(f"[缓存] 提取完成: {len(jobs)} 条原始, {len(slim_jobs)} 条去重后")
    print(f"[缓存] 字段精简为: {', '.join(KEEP_FIELDS)}")
    return slim_jobs


def extract_surface_since(last_count=0):
    """增量提取：只提取页面上 page_order >= last_count 的新岗位

    用于分批模式：第一批处理后，第二批只提取新增的岗位。

    参数:
        last_count: 之前已提取的岗位总数（新岗位的 page_order 从此开始）

    返回:
        list[dict]: 新增岗位列表（page_order 从 last_count 开始）
    """
    print(f"[缓存] 增量提取 (page_order >= {last_count})...")

    jobs = extract_jobs()

    # 补充计算字段
    for job in jobs:
        salary_str = job.get('salary', '')
        job['salary_max_k'] = parse_salary_max(salary_str)

    # 去重
    seen = set()
    unique_jobs = []
    for job in jobs:
        jid = job.get('jobId', '')
        if jid and jid not in seen:
            seen.add(jid)
            unique_jobs.append(job)

    # 只取 last_count 之后的新岗位
    if len(unique_jobs) <= last_count:
        print(f"[缓存] 无新增岗位 (总数{len(unique_jobs)}, 已提取{last_count})")
        return []

    new_jobs = unique_jobs[last_count:]
    for i, job in enumerate(new_jobs):
        job['page_order'] = last_count + i

    # 精简字段
    slim_jobs = []
    for job in new_jobs:
        slim = {k: job.get(k, '') for k in KEEP_FIELDS}
        slim_jobs.append(slim)

    print(f"[缓存] 新增 {len(slim_jobs)} 条 (page_order {last_count}~{last_count + len(slim_jobs) - 1})")
    return slim_jobs


def save_cache(jobs, filepath="surface_cache.json"):
    """保存岗位表面信息到缓存文件"""
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
    print(f"[缓存] 已保存 {len(jobs)} 条至 {filepath}")
    return filepath


def append_cache(new_jobs, filepath="surface_cache.json"):
    """追加新岗位到缓存文件（分批模式用）

    参数:
        new_jobs: 新增岗位列表
        filepath: 缓存文件路径

    返回:
        int: 追加后总条数
    """
    existing = []
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            existing = json.load(f)

    existing.extend(new_jobs)

    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    print(f"[缓存] 追加 {len(new_jobs)} 条，总计 {len(existing)} 条至 {filepath}")
    return len(existing)


def load_cache(filepath="surface_cache.json"):
    """加载缓存文件"""
    if not os.path.exists(filepath):
        print(f"[缓存] 文件不存在: {filepath}")
        return []
    with open(filepath, 'r', encoding='utf-8') as f:
        jobs = json.load(f)
    print(f"[缓存] 已加载 {len(jobs)} 条 from {filepath}")
    return jobs


def print_summary(jobs):
    """打印岗位缓存摘要"""
    if not jobs:
        print("缓存为空")
        return

    print("\n" + "=" * 50)
    print(f"岗位缓存摘要: 共 {len(jobs)} 条")
    print("=" * 50)

    # 薪资分布
    salary_ranges = {"<5K": 0, "5-10K": 0, "10-15K": 0, "15-20K": 0, "20K+": 0, "未知": 0}
    for j in jobs:
        sal = j.get('salary_max_k', 0)
        if sal == 0:
            salary_ranges["未知"] += 1
        elif sal < 5:
            salary_ranges["<5K"] += 1
        elif sal < 10:
            salary_ranges["5-10K"] += 1
        elif sal < 15:
            salary_ranges["10-15K"] += 1
        elif sal < 20:
            salary_ranges["15-20K"] += 1
        else:
            salary_ranges["20K+"] += 1

    print("\n薪资分布 (按上限):")
    for rng, count in salary_ranges.items():
        if count > 0:
            print(f"  {rng}: {count} 条")

    # 前10条预览
    print(f"\n前 10 条预览:")
    print(f"  {'#':>3}  {'order':>5}  {'标题':<25}  {'薪资':<12}  {'公司':<15}  {'行业'}")
    print(f"  {'-'*3}  {'-'*5}  {'-'*25}  {'-'*12}  {'-'*15}  {'-'*15}")
    for i, j in enumerate(jobs[:10], 1):
        title = (j.get('title', '') or '')[:25]
        salary = (j.get('salary', '') or '')[:12]
        company = (j.get('company', '') or '')[:15]
        industry = (j.get('industry', '') or '')[:15]
        order = j.get('page_order', i - 1)
        print(f"  {i:>3}  {order:>5}  {title:<25}  {salary:<12}  {company:<15}  {industry}")


def main():
    """CLI 入口: 批量提取表面信息 → 缓存文件"""
    import argparse
    parser = argparse.ArgumentParser(description="批量读取岗位表面信息 → 缓存文件")
    parser.add_argument("--output", default="surface_cache.json", help="输出缓存文件路径")
    args = parser.parse_args()

    # 提取
    jobs = extract_all_surface()

    if not jobs:
        print("未提取到任何岗位，请确认推荐页已加载")
        sys.exit(1)

    # 保存
    save_cache(jobs, args.output)

    # 摘要
    print_summary(jobs)


if __name__ == "__main__":
    main()
