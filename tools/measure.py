"""Measure warmed reads through an already running Cloud Workbench launcher."""
import argparse
import json
import math
import statistics
import time
from urllib import request


def read(url, rpc):
    message = json.dumps(rpc, ensure_ascii=False).encode()
    req = request.Request(url, data=message, headers={'Content-Type': 'application/json'})
    start = time.perf_counter()
    with request.urlopen(req, timeout=90) as response:
        payload = response.read()
    elapsed = (time.perf_counter() - start) * 1000
    result = json.loads(payload)
    if not result['ok']:
        raise SystemExit(result['error']['message'])
    return elapsed, len(payload), result['data']


def main():
    parser = argparse.ArgumentParser(description='测量工作台已运行时的文件读取时间')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--file', default='README.md')
    parser.add_argument('--samples', type=int, default=15)
    args = parser.parse_args()
    if not 1 <= args.samples <= 1000:
        parser.error('--samples 应在 1 到 1000 之间')
    url = f'http://127.0.0.1:{args.port}/api/rpc'
    mode = read(url, {'op':'status'})[2]['mode']
    rpc = {'op':'file', 'path':args.file}
    read(url, rpc)
    samples = []
    for _ in range(args.samples):
        elapsed, size, _ = read(url, rpc)
        samples.append({'ms': round(elapsed, 1), 'responseBytes': size})
    times = sorted(sample['ms'] for sample in samples)
    print(json.dumps({'mode': mode, 'path': args.file, 'count': len(times),
                      'medianMs': statistics.median(times),
                      'p95Ms': times[math.ceil(.95 * len(times))-1],
                      'samples': samples}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
