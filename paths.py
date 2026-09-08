# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Arnaud Ortais
import os

_BASE            = os.path.dirname(os.path.abspath(__file__))
DATA_DIR         = os.path.join(_BASE, "data")
CONFIG_PATH      = os.path.join(DATA_DIR, "config.json")
DB_PATH          = os.path.join(DATA_DIR, "protectado.db")
ACTION_QUEUE_DIR = "/tmp/fw-queue"
