"""
Analysis and visualization of network performance metrics.
Generates plots for RTT, jitter, packet loss, bandwidth, and prediction error.
"""

import json
import os


def load_metrics(filepath: str) -> dict:
    """Load a metrics JSON file."""
    with open(filepath) as f:
        return json.load(f)


def plot_latency_analysis(data: dict, output_dir: str = 'analysis'):
    """Generate comprehensive latency and jitter plots."""
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("[ANALYSIS] matplotlib/numpy not available. Skipping plots.")
        return

    os.makedirs(output_dir, exist_ok=True)

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle('Real-Time Multiplayer Network Analysis', fontsize=14, fontweight='bold')

    # ── 1. RTT over time ──
    ax = axes[0][0]
    rtts = data.get('rtt', [])
    if rtts:
        times = [r['t'] for r in rtts]
        values = [r['rtt_ms'] for r in rtts]
        ax.plot(times, values, linewidth=0.8, color='#2196F3')
        mean_rtt = np.mean(values)
        ax.axhline(y=mean_rtt, color='red', linestyle='--', linewidth=1,
                   label=f'Mean: {mean_rtt:.1f} ms')
        ax.fill_between(times, 0, values, alpha=0.1, color='#2196F3')
        ax.legend(fontsize=9)
    ax.set_title('Round-Trip Time (RTT)')
    ax.set_xlabel('Time (s)')
    ax.set_ylabel('RTT (ms)')
    ax.grid(True, alpha=0.3)

    # ── 2. Jitter over time ──
    ax = axes[0][1]
    jitters = data.get('jitter', [])
    if jitters:
        jtimes = [j['t'] for j in jitters]
        jvalues = [j['jitter_ms'] for j in jitters]
        instant = [j.get('instant_jitter_ms', 0) for j in jitters]
        ax.plot(jtimes, instant, linewidth=0.5, alpha=0.5, color='orange',
                label='Instantaneous')
        ax.plot(jtimes, jvalues, linewidth=1.5, color='red',
                label='Smoothed (RFC 3550)')
        ax.legend(fontsize=9)
    ax.set_title('Jitter')
    ax.set_xlabel('Time (s)')
    ax.set_ylabel('Jitter (ms)')
    ax.grid(True, alpha=0.3)

    # ── 3. RTT Distribution ──
    ax = axes[1][0]
    if rtts:
        values = [r['rtt_ms'] for r in rtts]
        ax.hist(values, bins=50, edgecolor='black', alpha=0.7, color='#4CAF50')
        arr = np.array(values)
        stats_text = (f'Mean: {np.mean(arr):.1f} ms\n'
                      f'Std:  {np.std(arr):.1f} ms\n'
                      f'P50:  {np.percentile(arr, 50):.1f} ms\n'
                      f'P95:  {np.percentile(arr, 95):.1f} ms\n'
                      f'P99:  {np.percentile(arr, 99):.1f} ms')
        ax.text(0.95, 0.95, stats_text, transform=ax.transAxes,
                verticalalignment='top', horizontalalignment='right',
                fontsize=9, family='monospace',
                bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.8))
    ax.set_title('RTT Distribution')
    ax.set_xlabel('RTT (ms)')
    ax.set_ylabel('Frequency')
    ax.grid(True, alpha=0.3)

    # ── 4. Packet Loss Rate ──
    ax = axes[1][1]
    losses = data.get('packet_loss', [])
    if losses:
        ltimes = [l['t'] for l in losses]
        lvals = [l['loss'] * 100 for l in losses]
        ax.plot(ltimes, lvals, linewidth=1.2, color='red')
        ax.fill_between(ltimes, 0, lvals, alpha=0.2, color='red')
    ax.set_title('Packet Loss Rate')
    ax.set_xlabel('Time (s)')
    ax.set_ylabel('Loss (%)')
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    path = os.path.join(output_dir, 'network_analysis.png')
    plt.savefig(path, dpi=150)
    print(f"[ANALYSIS] Saved: {path}")
    plt.close()


def plot_prediction_error(data: dict, output_dir: str = 'analysis'):
    """Plot client-side prediction error over time."""
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return

    os.makedirs(output_dir, exist_ok=True)
    errors = data.get('prediction_error', [])
    if not errors:
        print("[ANALYSIS] No prediction error data.")
        return

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    fig.suptitle('Client-Side Prediction Error Analysis', fontsize=13)

    # Error over time
    times = [e['t'] for e in errors]
    vals = [e['error_px'] for e in errors]
    axes[0].plot(times, vals, linewidth=0.8, color='purple')
    axes[0].set_title('Prediction Error Over Time')
    axes[0].set_xlabel('Time (s)')
    axes[0].set_ylabel('Error (pixels)')
    axes[0].grid(True, alpha=0.3)

    # CDF
    sorted_vals = np.sort(vals)
    cdf = np.arange(1, len(sorted_vals) + 1) / len(sorted_vals)
    axes[1].plot(sorted_vals, cdf * 100, linewidth=1.5, color='purple')
    axes[1].set_title('Prediction Error CDF')
    axes[1].set_xlabel('Error (pixels)')
    axes[1].set_ylabel('Percentile (%)')
    axes[1].grid(True, alpha=0.3)

    plt.tight_layout()
    path = os.path.join(output_dir, 'prediction_error_analysis.png')
    plt.savefig(path, dpi=150)
    print(f"[ANALYSIS] Saved: {path}")
    plt.close()


def plot_bandwidth(data: dict, output_dir: str = 'analysis'):
    """Plot bandwidth usage over time."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return

    os.makedirs(output_dir, exist_ok=True)
    bw = data.get('bandwidth', [])
    if not bw:
        return

    fig, ax = plt.subplots(figsize=(10, 4))
    times = [b['t'] for b in bw]
    sent = [b['sent_bytes'] / 1024 for b in bw]
    recv = [b['recv_bytes'] / 1024 for b in bw]
    ax.plot(times, sent, label='Sent (KB/s)', color='#2196F3')
    ax.plot(times, recv, label='Received (KB/s)', color='#4CAF50')
    ax.set_title('Bandwidth Usage')
    ax.set_xlabel('Time (s)')
    ax.set_ylabel('KB/s')
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    path = os.path.join(output_dir, 'bandwidth_analysis.png')
    plt.savefig(path, dpi=150)
    print(f"[ANALYSIS] Saved: {path}")
    plt.close()


def plot_tick_times(data: dict, output_dir: str = 'analysis'):
    """Plot server tick processing times."""
    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        return

    os.makedirs(output_dir, exist_ok=True)
    ticks = data.get('tick_times', [])
    if not ticks:
        return

    fig, ax = plt.subplots(figsize=(10, 4))
    tick_nums = [t['tick'] for t in ticks]
    durations = [t['duration_ms'] for t in ticks]
    ax.plot(tick_nums, durations, linewidth=0.5, color='#FF5722')
    mean_d = np.mean(durations)
    ax.axhline(y=mean_d, color='blue', linestyle='--',
               label=f'Mean: {mean_d:.3f} ms')
    ax.set_title('Server Tick Processing Time')
    ax.set_xlabel('Tick')
    ax.set_ylabel('Duration (ms)')
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    path = os.path.join(output_dir, 'tick_time_analysis.png')
    plt.savefig(path, dpi=150)
    print(f"[ANALYSIS] Saved: {path}")
    plt.close()


def analyze_all(filepath: str, output_dir: str = 'analysis'):
    """Run all analysis on a metrics file."""
    import numpy as np

    print(f"[ANALYSIS] Loading {filepath}...")
    data = load_metrics(filepath)

    plot_latency_analysis(data, output_dir)
    plot_prediction_error(data, output_dir)
    plot_bandwidth(data, output_dir)
    plot_tick_times(data, output_dir)

    # Print summary
    print("\n=== Metrics Summary ===")
    rtts = [r['rtt_ms'] for r in data.get('rtt', [])]
    if rtts:
        arr = np.array(rtts)
        print(f"  RTT:    mean={np.mean(arr):.1f} ms, "
              f"std={np.std(arr):.1f} ms, "
              f"P95={np.percentile(arr, 95):.1f} ms, "
              f"P99={np.percentile(arr, 99):.1f} ms")

    jitters = [j['jitter_ms'] for j in data.get('jitter', [])]
    if jitters:
        print(f"  Jitter: mean={np.mean(jitters):.1f} ms")

    losses = [l['loss'] for l in data.get('packet_loss', [])]
    if losses:
        print(f"  Loss:   mean={np.mean(losses)*100:.2f}%")

    errors = [e['error_px'] for e in data.get('prediction_error', [])]
    if errors:
        print(f"  Pred Error: mean={np.mean(errors):.1f} px, "
              f"P95={np.percentile(errors, 95):.1f} px")

    ticks = [t['duration_ms'] for t in data.get('tick_times', [])]
    if ticks:
        print(f"  Tick Time:  mean={np.mean(ticks):.3f} ms, "
              f"max={np.max(ticks):.3f} ms")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Analyze game metrics')
    parser.add_argument('file', help='Metrics JSON file to analyze')
    parser.add_argument('--output', default='analysis', help='Output directory')
    args = parser.parse_args()
    analyze_all(args.file, args.output)
