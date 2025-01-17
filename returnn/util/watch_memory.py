"""
Watch memory usage over time.
"""

from __future__ import annotations

import os
import sys
from typing import Dict
import time
from datetime import datetime
from collections import defaultdict
import multiprocessing
import psutil  # noqa


def watch_memory():
    """
    Start thread which watches memory usage over time of the current process and all its children over time.
    """
    global _watch_memory_proc
    if _watch_memory_proc:
        return
    _watch_memory_proc = multiprocessing.get_context("spawn").Process(
        target=_watch_memory_main, args=(os.getpid(),), name="watch_memory", daemon=True
    )
    _watch_memory_proc.start()


_watch_memory_proc = None


def _watch_memory_main(pid: int):
    if sys.platform == "linux":
        with open("/proc/self/comm", "w") as f:
            f.write(f"watch memory")

    def _print(*args):
        print("MEMORY:", *args)
        sys.stdout.flush()

    cur_proc = psutil.Process(pid)
    procs = []
    mem_per_pid = {}

    while True:
        change = False
        procs_ = [cur_proc] + cur_proc.children(recursive=True)
        for p in procs:
            if p not in procs_:
                _print(f"proc {_format_proc(p)} exited, old:", _format_mem_info(mem_per_pid[p.pid]))
                mem_per_pid.pop(p.pid, None)
                change = True
        procs = procs_

        for p in list(procs):
            old_mem_info = mem_per_pid.get(p.pid, None)
            try:
                mem_info = get_mem_info(p)
            except psutil.NoSuchProcess:  # race condition, can happen
                if old_mem_info:
                    _print(f"proc {_format_proc(p)} exited, old:", _format_mem_info(old_mem_info))
                    mem_per_pid.pop(p.pid, None)
                    change = True
                procs.remove(p)
                continue
            proc_prefix = "main" if p == cur_proc else "sub"
            if not old_mem_info:
                _print(f"{proc_prefix} proc {_format_proc(p)} initial:", _format_mem_info(mem_info))
                mem_per_pid[p.pid] = mem_info
                change = True
            elif mem_info["rss"] > old_mem_info["rss"] and _format_mem_size(old_mem_info["rss"]) != _format_mem_size(
                mem_info["rss"]
            ):
                _print(f"{proc_prefix} proc {_format_proc(p)} increased RSS:", _format_mem_info(mem_info))
                # keep old info otherwise, such that the update check works
                mem_per_pid[p.pid] = mem_info
                change = True

        if change:
            res = {"pss": 0, "uss": 0}
            for mem_info in mem_per_pid.values():
                for k in res.keys():
                    res[k] += mem_info[k]
            _print(
                "total",
                f"(main {cur_proc.pid}, {datetime.now().strftime('%Y-%m-%d, %H:%M:%S')}, {len(mem_per_pid)} procs):",
                _format_mem_info(res),
            )

        time.sleep(5)


def _format_proc(proc: psutil.Process) -> str:
    try:
        proc_name = proc.name()
    except psutil.NoSuchProcess:  # race condition
        proc_name = getattr(proc, "_name", None)
        if not proc_name:
            proc_name = "<unknown-dead>"
    if not proc_name:
        proc_name = "<noname>"
    return "%s(%s)" % (proc_name, proc.pid)


def _format_mem_info(info: Dict[str, int]) -> str:
    return " ".join("%s=%s" % (k, _format_mem_size(v)) for (k, v) in info.items())


def _format_mem_size(c: int) -> str:
    if c < 1024:
        return "%iB" % c
    units = "KMG"
    i = 0
    while i < len(units) - 1:
        if c < 0.8 * 1024 ** (i + 2):
            break
        i += 1
    f = float(c) / (1024 ** (i + 1))
    return "%.1f%sB" % (f, units[i])


def get_mem_info(proc: psutil.Process) -> Dict[str, int]:
    """
    Code from:
    https://ppwwyyxx.com/blog/2022/Demystify-RAM-Usage-in-Multiprocess-DataLoader/
    """
    res = defaultdict(int)
    for mmap in proc.memory_maps():
        res["rss"] += mmap.rss
        res["pss"] += mmap.pss
        res["uss"] += mmap.private_clean + mmap.private_dirty
        res["shared"] += mmap.shared_clean + mmap.shared_dirty
    return res
