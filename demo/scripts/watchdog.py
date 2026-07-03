#!/usr/bin/env python3
"""Watchdog: monitor benchmark progress and alert on stalls/crashes."""
import json, os, time, glob, sys

BENCHMARK_PID = 108760
RESULTS_DIR = '/home/teddy/enterprise-nl2sql/bird_bench/results/full_benchmarks'
CHECK_INTERVAL = 600  # 10 min
STALL_THRESHOLD = 1800  # 30 min without progress = stalled

def check_progress():
    progress_files = sorted(glob.glob(f'{RESULTS_DIR}/*sample_220_indices_progress*'))
    result_files = sorted(glob.glob(f'{RESULTS_DIR}/*sample_220_indices*.json'))
    
    proc_alive = os.path.exists(f'/proc/{BENCHMARK_PID}')
    
    latest = None
    if progress_files:
        with open(progress_files[-1]) as f:
            latest = json.load(f)
        done = len(latest.get('results', []))
        passed = latest.get('passed', 0)
    else:
        done = 0
        passed = 0
    
    # Check if a final result file exists (benchmark completed)
    final = None
    for f in result_files:
        if 'progress' not in f:
            with open(f) as fh:
                d = json.load(fh)
            if d.get('total', 0) >= 200:  # completed or near-complete
                final = d
    
    return proc_alive, done, passed, latest, final

def main():
    last_done = 0
    first_run = time.time()
    
    while True:
        proc_alive, done, passed, latest, final = check_progress()
        elapsed = int((time.time() - first_run) / 60)
        
        now = time.strftime('%H:%M:%S')
        
        if final:
            print(f"[{now}] ✅ BENCHMARK COMPLETE! EX={final['ex']}% ({final['passed']}/{final['total']}) in {final.get('time_min', '?')}min")
            return 0
        
        if not proc_alive:
            # Check if there's a final file with any data
            result_files = sorted(glob.glob(f'{RESULTS_DIR}/*sample_220_indices*.json'))
            result_excl_progress = [f for f in result_files if 'progress' not in f]
            if result_excl_progress:
                with open(result_excl_progress[-1]) as f:
                    d = json.load(f)
                print(f"[{now}] ⚠️ PROCESS DEAD. Partial results: {d.get('passed',0)}/{d.get('total',0)} (EX={d.get('ex','?')}%)")
                return 1
            else:
                print(f"[{now}] ⚠️ PROCESS DEAD — NO RESULTS FILE. Something went wrong early.")
                return 1
        
        # Check for stall
        since_last = done - last_done
        if last_done > 0 and since_last == 0 and elapsed > 30:
            # Check file modification time
            progress_files = sorted(glob.glob(f'{RESULTS_DIR}/*sample_220_indices_progress*'))
            if progress_files:
                mtime = os.path.getmtime(progress_files[-1])
                age = time.time() - mtime
                if age > STALL_THRESHOLD:
                    print(f"[{now}] 🚨 STALLED! No progress for {age/60:.0f}min. Last checkpoint: {done}/220 done, {passed} ✅ ({passed/max(done,1)*100:.1f}%)")
                    return 2
        
        ex = passed / max(done, 1) * 100
        repair_count = sum(1 for r in (latest.get('results', []) if latest else []) if r.get('repair_used')) if latest else 0
        print(f"[{now}] ⏳ {done:>3}/220 | {passed} ✅ ({ex:.1f}%) | {repair_count} repairs | {elapsed}min elapsed | +{since_last} since last check")
        
        last_done = done
        time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    sys.exit(main())
