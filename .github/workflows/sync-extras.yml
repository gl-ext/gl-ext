name: sync-extras

on:
  workflow_dispatch:
    inputs:
      args:
        description: "Доп. аргументы (например --dry-run или --force)"
        required: false
        default: ""
  schedule:
    - cron: "0 5 * * *"   # раз в сутки, 05:00 UTC

permissions:
  contents: write

concurrency:
  group: sync-extras
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Run sync_extras.py
        run: python scripts/sync_extras.py --commit ${{ github.event.inputs.args }}
