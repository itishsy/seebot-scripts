# Seebot Statistics Scripts

Daily statistics scripts for RPA execution reporting.

## Scripts

- `robot_daily_report.py`: reads MySQL task queue data and Mongo execution details, then generates daily summary, failure Top10, invalid-run summary, invalid-run details, and raw task sheets.
- `standalone_daily_report.py`: reads standalone deployment data through REST APIs and generates the standalone daily report.

## Install

```bash
pip install -r requirements.txt
```

## Configuration

Copy `db.conf.example` to `db.conf` and fill local values:

```bash
cp db.conf.example db.conf
```

`db.conf` is ignored by git because it contains database and API credentials.

## Usage

```bash
python robot_daily_report.py --date 2026-05-31
python standalone_daily_report.py --date 2026-05-31
```

Use `--output` to specify the report path, and `--config` to specify a config file path.

