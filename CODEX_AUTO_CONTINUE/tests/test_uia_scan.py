"""UIA 어댑터의 화면 해석 로직을 가짜 UIA 요소 트리로 검증 (pywinauto/Windows 불필요)."""
import sys
import types
import unittest

from helpers import dt
from codex_auto_continue.backend_uia import UiaBackend
from codex_auto_continue.config import Config
from codex_auto_continue.detector import detect_limit
from codex_auto_continue.identify import identify
from codex_auto_continue.models import Snapshot, Target


class R:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b


class Info:
    def __init__(self, ct, name, rect, parent=None, selected=False, value=None, enabled=True):
        self.control_type, self.name, self.rectangle = ct, name, R(*rect)
        self.parent, self.selected, self.value = parent, selected, value
        self.enabled, self.visible = enabled, True


class FakeWin:
    def __init__(self, title, infos):
        self._title = title
        self.element_info = types.SimpleNamespace(rectangle=R(0, 0, 1200, 800), descendants=lambda: infos)

    def window_text(self):
        return self._title


class FakeWrapper:
    def __init__(self, info):
        self.info = info

    @property
    def iface_selection_item(self):
        return types.SimpleNamespace(CurrentIsSelected=self.info.selected)

    @property
    def iface_value(self):
        return types.SimpleNamespace(CurrentValue=self.info.value)

    def window_text(self):
        return self.info.value or ""


def install_fake_pywinauto():
    mods = {}
    for n in ("pywinauto", "pywinauto.controls", "pywinauto.controls.uiawrapper"):
        mods[n] = types.ModuleType(n)
    mods["pywinauto.controls.uiawrapper"].UIAWrapper = FakeWrapper
    saved = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    return saved


def restore(saved):
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def build_tree(active="MY AI OFFICE 구축하기", banner=None, busy=False, draft=""):
    grp = Info("Group", "MY AI OFFICE 구축하기", (0, 60, 280, 400))          # 프로젝트 그룹(조상)
    other_grp = Info("Group", "다른 프로젝트", (0, 400, 280, 700))
    items = [
        Info("ListItem", "MY AI OFFICE 구축하기", (10, 90, 270, 120), parent=grp,
             selected=(active == "MY AI OFFICE 구축하기")),
        Info("ListItem", "다른 대화 A", (10, 130, 270, 160), parent=other_grp, selected=(active == "다른 대화 A")),
        Info("Text", "MY AI OFFICE 구축하기", (20, 95, 200, 115)),                  # 사이드바 텍스트(증거 아님)
        Info("Text", active, (400, 20, 900, 50)),                                    # 상단 제목
        Info("Text", "이전 작업 내용입니다", (320, 300, 900, 330)),
        Info("Edit", "", (320, 700, 1100, 760), value=draft),
        Info("Button", "전송", (1110, 710, 1150, 750)),
    ]
    if banner:
        items.append(Info("Text", banner, (320, 650, 1100, 690)))
    if busy:
        items.append(Info("Button", "중지", (1050, 710, 1100, 750)))
    return FakeWin(f"{active} - Codex", items)


BANNER = "Codex 및 Work 사용량을 모두 사용했습니다. 요청 한도는 오전 11:35에 재설정됩니다."
T = Target("MY AI OFFICE 구축하기", "MY AI OFFICE 구축하기")


class UiaScan(unittest.TestCase):
    def setUp(self):
        self._saved = install_fake_pywinauto()
        self.be = UiaBackend(Config())

    def tearDown(self):
        restore(self._saved)

    def scan(self, win):
        snap = Snapshot(found=True, title=win.window_text())
        self.be._scan(win, snap)
        return snap

    def test_reads_banner_and_time(self):
        snap = self.scan(build_tree(banner=BANNER))
        det = detect_limit(snap.banner_texts, dt(9))
        self.assertEqual(det.reset.reset_at, dt(11, 35))
        self.assertTrue(snap.input_found)
        self.assertEqual(snap.input_text, "")
        self.assertFalse(snap.busy)

    def test_old_chat_text_not_in_banner_zone(self):
        snap = self.scan(build_tree(banner=BANNER))
        self.assertNotIn("이전 작업 내용입니다", snap.banner_texts)

    def test_identifies_target(self):
        self.assertTrue(identify(self.scan(build_tree()), T).ok)

    def test_other_conversation_not_mistaken_for_target(self):
        # '다른 대화 A' 가 열려 있고, 사이드바에는 대상 제목이 (선택 안 된 채) 보임 → 식별 실패여야 함
        snap = self.scan(build_tree(active="다른 대화 A"))
        self.assertFalse(identify(snap, T).ok, snap.conversation_candidates)

    def test_busy_by_stop_button(self):
        self.assertTrue(self.scan(build_tree(busy=True)).busy)

    def test_reads_user_draft(self):
        self.assertEqual(self.scan(build_tree(draft="작성 중인 글")).input_text, "작성 중인 글")

    def test_placeholder_is_not_a_draft(self):
        cfg = Config(); cfg.selectors["input_placeholders"] = ["후속 변경 사항을 부탁하세요"]
        be = UiaBackend(cfg)
        snap = Snapshot(found=True)
        be._scan(build_tree(draft="후속 변경 사항을 부탁하세요"), snap)
        self.assertEqual(snap.input_text, "")

    def test_project_evidence_from_ancestors(self):
        t = Target("MY AI OFFICE 구축하기", "MY AI OFFICE 구축하기")
        self.assertTrue(identify(self.scan(build_tree()), t, require_project=True).ok)


if __name__ == "__main__":
    unittest.main()
