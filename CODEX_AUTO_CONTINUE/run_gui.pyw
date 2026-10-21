# 콘솔 없이 실행하는 진입점 (pythonw.exe 가 사용, 자동 실행 등록에도 사용됨)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from codex_auto_continue.__main__ import main  # noqa: E402

sys.exit(main(["gui"] + sys.argv[1:]))
