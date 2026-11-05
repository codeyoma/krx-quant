"""``python -m krx_quant.cli``: how the engine starts a command in a process of its own (the nightly
training and the scheduled jobs, ``process_log.KRX_QUANT``)."""

import sys

from krx_quant.cli import main

sys.exit(main())
