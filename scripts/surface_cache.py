#!/usr/bin/env python3


























import json
try:
    from scripts.atomic_io import atomic_dump_json
except ImportError:
    from atomic_io import atomic_dump_json

import os
import sys
import time

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_DIR)

from scripts.webbridge_client import (
    SESSION, evaluate, extract_jobs, decrypt_salary, parse_salary_max,
)


KEEP_FIELDS = ['page_order', 'jobId', 'title', 'salary', 'salary_max_k', 'company', 'industry', 'city']


def extract_all_surface():








    print("[缓存] 开始批量提取岗位表面信息...")


    jobs = extract_jobs()


    for job in jobs:
        salary_str = job.get('salary', '')
        job['salary_max_k'] = parse_salary_max(salary_str)


    seen = set()
    unique_jobs = []
    for job in jobs:
        jid = job.get('jobId', '')
        if jid and jid not in seen:
            seen.add(jid)
            unique_jobs.append(job)


    for i, job in enumerate(unique_jobs):
        job['page_order'] = i


    slim_jobs = []
    for job in unique_jobs:
        slim = {k: job.get(k, '') for k in KEEP_FIELDS}
        slim_jobs.append(slim)

    print(f"[缓存] 提取完成: {len(jobs)} 条原始, {len(slim_jobs)} 条去重后")
    print(f"[缓存] 字段精简为: {', '.join(KEEP_FIELDS)}")
    return slim_jobs


def extract_surface_since(last_count=0):










    print(f"[缓存] 增量提取 (page_order >= {last_count})...")

    jobs = extract_jobs()


    for job in jobs:
        salary_str = job.get('salary', '')
        job['salary_max_k'] = parse_salary_max(salary_str)


    seen = set()
    unique_jobs = []
    for job in jobs:
        jid = job.get('jobId', '')
        if jid and jid not in seen:
            seen.add(jid)
            unique_jobs.append(job)


    if len(unique_jobs) <= last_count:
        print(f"[缓存] 无新增岗位 (总数{len(unique_jobs)}, 已提取{last_count})")
        return []

    new_jobs = unique_jobs[last_count:]
    for i, job in enumerate(new_jobs):
        job['page_order'] = last_count + i


    slim_jobs = []
    for job in new_jobs:
        slim = {k: job.get(k, '') for k in KEEP_FIELDS}
        slim_jobs.append(slim)

    print(f"[缓存] 新增 {len(slim_jobs)} 条 (page_order {last_count}~{last_count + len(slim_jobs) - 1})")
    return slim_jobs


def save_cache(jobs, filepath="surface_cache.json"):

    atomic_dump_json(filepath, jobs, indent=2)
    print(f"[缓存] 已保存 {len(jobs)} 条至 {filepath}")
    return filepath


def append_cache(new_jobs, filepath="surface_cache.json"):









    existing = []
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            existing = json.load(f)

    existing.extend(new_jobs)

    atomic_dump_json(filepath, existing, indent=2)

    print(f"[缓存] 追加 {len(new_jobs)} 条，总计 {len(existing)} 条至 {filepath}")
    return len(existing)


def load_cache(filepath="surface_cache.json"):

    if not os.path.exists(filepath):
        print(f"[缓存] 文件不存在: {filepath}")
        return []
    with open(filepath, 'r', encoding='utf-8') as f:
        jobs = json.load(f)
    print(f"[缓存] 已加载 {len(jobs)} 条 from {filepath}")
    return jobs


def print_summary(jobs):

    if not jobs:
        print("缓存为空")
        return

    print("\n" + "=" * 50)
    print(f"岗位缓存摘要: 共 {len(jobs)} 条")
    print("=" * 50)


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

    import argparse
    parser = argparse.ArgumentParser(description="批量读取岗位表面信息 → 缓存文件")
    parser.add_argument("--output", default="surface_cache.json", help="输出缓存文件路径")
    args = parser.parse_args()


    jobs = extract_all_surface()

    if not jobs:
        print("未提取到任何岗位，请确认推荐页已加载")
        sys.exit(1)


    save_cache(jobs, args.output)


    print_summary(jobs)


if __name__ == "__main__":
    main()