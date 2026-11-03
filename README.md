# Linux Health Reporter

[![Python tests](https://github.com/BRMff/linux-health-reporter/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/BRMff/linux-health-reporter/actions/workflows/tests.yml)

**A lightweight, read-only Python CLI for Linux health snapshots — in JSON or Markdown.**

Get a quick view of system load, memory, swap, root filesystem space, uptime and kernel pressure stall information (PSI) without deploying a monitoring service. Output is written to standard output, ready to inspect or use in scripts. The runtime uses only the Python standard library.

**Releases:** [GitHub releases](https://github.com/BRMff/linux-health-reporter/releases) · **Changes:** [CHANGELOG](CHANGELOG.md) · **License:** [MIT](LICENSE)

## What it reports

| Metric | Source |
| --- | --- |
| Load average (1, 5 and 15 minutes) | `/proc/loadavg` |
| Total and available RAM | `/proc/meminfo` |
| Total and free swap | `/proc/meminfo` |
| Root filesystem capacity and available space | `os.statvfs("/")` |
| System uptime | `/proc/uptime` |
| CPU, memory and I/O pressure (PSI) | `/proc/pressure/*` |
| UTC collection timestamp | Python clock |

- **JSON** is the stable, versioned interface for scripts; its contract is documented in [docs/schema.md](docs/schema.md). **Markdown** is a readable summary whose layout may change.
- Each metric has an explicit status: `ok`, `unavailable` (collection failed; values are `null`, never invented zeros) or, for PSI only, `unsupported` (the kernel does not expose the interface).
- The fixed report schema excludes hostnames, IP addresses, usernames, device names, paths and raw exception messages. `/proc/swaps` is never read.
- Collection is read-only and does not start services, execute shell commands, contact a network or write reports to disk.

## Quick start

**Requirements:** Linux and Python 3.10 or newer.

Run from a clone, without installing:

```bash
git clone https://github.com/BRMff/linux-health-reporter.git
cd linux-health-reporter
python3 -B -m linux_health_reporter --format json
python3 -B -m linux_health_reporter --format markdown
```

Or install the command from the clone (for example with `pipx install .`) and run `linux-health-reporter --format markdown`.

`-B` prevents Python bytecode-cache writes. The CLI prints to stdout; saving output is an explicit choice.

| Exit code | Meaning |
| --- | --- |
| `0` | Complete snapshot (PSI marked `unsupported` still counts as complete) |
| `1` | The timestamp or at least one applicable reading is unavailable |
| `2` | Invalid command-line arguments |

## Example output

Both examples use the **same synthetic fixture**, not measurements taken from a real computer.

### Markdown

```text
# Linux Health Report

Generated (UTC): 2026-01-02T03:04:05Z

| Metric | Value |
| --- | --- |
| Load average (1m / 5m / 15m) | 0.42 / 0.36 / 0.31 |
| Memory available | 8.8 GiB of 15.6 GiB (56.2 %) |
| Swap free | 1.9 GiB of 2.0 GiB (95.0 %) |
| Root filesystem available | 238.4 GiB of 476.8 GiB (50.0 %) |
| Uptime | 1 d 00 h 00 min |

## Pressure stall information

Share of time tasks were stalled, averaged over 10 s / 60 s / 300 s.

| Resource | Some | Full |
| --- | --- | --- |
| CPU | 1.25 / 0.80 / 0.40 % | n/a |
| Memory | 0.50 / 0.25 / 0.10 % | 0.20 / 0.10 / 0.05 % |
| I/O | 3.00 / 2.50 / 1.75 % | 1.50 / 1.25 / 0.75 % |
```

### JSON (abridged)

```json
{
  "schema_version": "0.2",
  "timestamp_utc": "2026-01-02T03:04:05Z",
  "memory": {"status": "ok", "total_bytes": 16777216000, "available_bytes": 9437184000},
  "swap": {"status": "ok", "total_bytes": 2147483648, "free_bytes": 2040109056},
  "pressure": {
    "cpu": {"status": "ok", "some": {"avg10": 1.25, "avg60": 0.8, "avg300": 0.4}},
    "memory": {"status": "ok", "some": {"avg10": 0.5, "avg60": 0.25, "avg300": 0.1}, "full": {"avg10": 0.2, "avg60": 0.1, "avg300": 0.05}},
    "io": {"status": "ok", "some": {"avg10": 3.0, "avg60": 2.5, "avg300": 1.75}, "full": {"avg10": 1.5, "avg60": 1.25, "avg300": 0.75}}
  }
}
```

The full report also contains `load_average`, `disk_root` and `uptime`; see the [complete synthetic fixture](examples/sample.json).

## Testing and engineering

**CI:** GitHub Actions runs the synthetic unit tests on Python **3.10 to 3.14**, then builds the sdist and wheel with pinned, hash-verified build tools, installs the wheel in a separate clean environment and smoke-tests the installed CLI. The smoke test does not run a collection on the CI runner. Actions are pinned by commit SHA; Dependabot proposes updates weekly.

**Governance:** changes reach `main` through pull requests with a required owner review gate, and release tags go through a preflight and post-tag verification. See [docs/governance.md](docs/governance.md) and [docs/release-process.md](docs/release-process.md), including what these controls do not guarantee.

Run the tests locally:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
```

This project is developed with **human-defined requirements and approvals, AI-assisted implementation, synthetic regression tests and a separate model review**. The application itself is a conventional CLI; it does not run an AI model.

See the [engineering workflow and validation evidence](docs/engineering-workflow.md) for the review process and the scope of each piece of evidence.

## Known limitations

- **Experimental utility**, not a production-certified monitoring system.
- Tests use injected synthetic `/proc`, filesystem and clock inputs. They verify parsing, classification and output, **not** live metrics on real hardware.
- Inside containers, memory, swap and PSI values often describe the host unless the runtime virtualises them.
- Load average is not CPU utilisation. Filesystem capacity covers only the filesystem containing `/`, and available space is reported for an unprivileged user.
- There is no scheduler, daemon, network reporting or background monitoring.
- Even without identifying fields, real timestamps and measurements may reveal usage patterns. **Review reports before sharing them publicly.**

## License

[MIT License](LICENSE) · Copyright © 2026 Fábio Folgado.
