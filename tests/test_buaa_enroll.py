import io
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import Mock, patch

from selenium.webdriver.remote.webelement import WebElement

import buaa_enroll as enroll


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.target = {
            "menu_name": "培养方案外课程",
            "course_code": "TEST001",
            "serial_code": None,
            "course_name": "",
        }

    def mock(self, name, **kwargs):
        return self.stack.enter_context(patch.object(enroll, name, **kwargs))

    def result_messages(self, messages):
        self.mock("visible_elements", side_effect=lambda d, by, selector: (
            [Mock(text=message) for message in messages]
            if selector == ".el-message__content" else []
        ))
        self.stack.enter_context(patch.object(enroll.time, "monotonic", side_effect=[0, 0, 2]))
        self.stack.enter_context(patch.object(enroll.time, "sleep"))

    def prepare_selection(self):
        driver = Mock()
        row = Mock()
        button = Mock(text="选择")
        row.find_elements.return_value = [button]
        search = self.mock("search_courses", return_value=[row])
        self.mock("course_identity", return_value=("TEST001", "002"))
        self.mock("operation_text", return_value="选择")
        self.mock("capacity_status", return_value={
            "selected_by_me": False, "capacity": "10/10", "selected": "0/0",
            "remaining": "10/10", "quota_name": "对外", "quota_remaining": 10,
            "available": True,
        })
        self.mock("WebDriverWait")
        self.mock("confirm_normal_selection")
        self.mock("wait_for_result", return_value=True)
        return driver, row, button, search

    def test_negative_success_word_is_failure(self):
        self.result_messages(["选课未成功，请重试"])
        self.assertFalse(enroll.wait_for_result(Mock(), 1))

    def test_explicit_success_is_candidate_for_verification(self):
        self.result_messages(["选课成功！"])
        self.assertTrue(enroll.wait_for_result(Mock(), 1))

    def test_queue_timeout_requires_manual_check(self):
        self.result_messages(["成功进入选课队列"])
        with self.assertRaisesRegex(enroll.ManualActionRequired, "已进入队列"):
            enroll.wait_for_result(Mock(), 1)

    def test_unrelated_success_does_not_complete_selection(self):
        self.result_messages(["查询成功"])
        with self.assertRaises(enroll.ManualActionRequired):
            enroll.wait_for_result(Mock(), 1)

    def test_missing_result_does_not_retry(self):
        self.result_messages([])
        with self.assertRaises(enroll.ManualActionRequired):
            enroll.wait_for_result(Mock(), 1)

    def test_queue_followed_by_success(self):
        self.result_messages(["进入选课队列", "选课成功"])
        self.assertTrue(enroll.wait_for_result(Mock(), 1))

    def test_success_requires_selected_class(self):
        driver, row, button, search = self.prepare_selection()
        enroll.operation_text.side_effect = ["选择", "退选"]
        self.assertTrue(enroll.choose_target_once(driver, self.target, 1))
        search.assert_called_with(driver, "TEST001", "002")
        button.click.assert_called_once()
        enroll.wait_for_result.assert_called_once_with(driver, 1)
        enroll.confirm_normal_selection.assert_not_called()

    def test_success_without_selected_state_pauses(self):
        driver, row, button, search = self.prepare_selection()
        with self.assertRaisesRegex(enroll.ManualActionRequired, "未查到已选状态"):
            enroll.choose_target_once(driver, self.target, 1)
        button.click.assert_called_once()

    def test_timeout_after_click_does_not_try_another_course(self):
        driver, row, button, search = self.prepare_selection()
        search.return_value = [row, Mock()]
        enroll.wait_for_result.side_effect = enroll.TimeoutException()
        with self.assertRaises(enroll.ManualActionRequired):
            enroll.choose_target_once(driver, self.target, 1)
        button.click.assert_called_once()
        search.assert_called_once()

    def test_disabled_choose_button_is_skipped(self):
        driver, row, button, search = self.prepare_selection()
        button.is_enabled.return_value = False
        self.assertFalse(enroll.choose_target_once(driver, self.target, 1))
        button.click.assert_not_called()

    def test_known_floating_tool_is_removed_before_retry(self):
        driver, button, blocker = Mock(), Mock(), Mock()
        driver.execute_script.side_effect = [None, blocker, None]
        button.click.side_effect = [enroll.ElementClickInterceptedException(), None]
        enroll.click_choose_button(driver, button)
        self.assertEqual(button.click.call_count, 2)
        self.assertEqual(driver.execute_script.call_args.args[1:], (blocker,))
        self.assertIn("arguments[0].remove()", driver.execute_script.call_args.args[0])

    def test_unknown_overlay_is_not_bypassed(self):
        driver, button = Mock(), Mock()
        driver.execute_script.side_effect = [None, None]
        button.click.side_effect = enroll.ElementClickInterceptedException()
        with self.assertRaises(enroll.ManualActionRequired):
            enroll.click_choose_button(driver, button)
        button.click.assert_called_once()

    def test_repeated_interception_after_toolbar_removal_pauses(self):
        driver, row, button, search = self.prepare_selection()
        blocker = Mock()
        driver.execute_script.side_effect = [None, blocker, None]
        button.click.side_effect = enroll.ElementClickInterceptedException()
        with self.assertRaises(enroll.ManualActionRequired):
            enroll.choose_target_once(driver, self.target, 1)
        self.assertEqual(button.click.call_count, 2)
        self.assertEqual(driver.execute_script.call_args.args[1:], (blocker,))
        enroll.confirm_normal_selection.assert_not_called()

    def test_invalid_settings_are_rejected(self):
        read = self.mock("read_json")
        for key, value in (
            ("retry_interval", "NaN"), ("result_timeout", "inf"),
            ("login_reminder_interval", 0), ("page_reload_every", 1.5),
            ("retry_interval", True), ("retry_interval", -1),
        ):
            with self.subTest(key=key, value=value):
                read.return_value = {
                    "courses": [{"type": "体育", "code": "TEST001"}],
                    "settings": {key: value},
                }
                with self.assertRaises(ValueError):
                    enroll.load_config(None)

    def test_existing_configuration_normalization_is_preserved(self):
        self.mock("read_json", return_value={
            "courses": [{"type": "体育", "code": " test001 ", "serial": 1}],
            "settings": {"page_reload_every": 0, "retry_interval": "0.5"},
        })
        targets, settings = enroll.load_config(None)
        self.assertEqual(targets[0], {
            "menu_name": "体育课", "course_code": "TEST001",
            "serial_code": "001", "course_name": "",
        })
        self.assertEqual(settings["page_reload_every"], 0)
        self.assertEqual(settings["retry_interval"], 0.5)

    def test_null_credentials_and_course_code_are_rejected(self):
        read = self.mock("read_json")
        for username, password in ((None, "secret"), ("student", None)):
            read.return_value = {"username": username, "password": password}
            with self.assertRaises(ValueError):
                enroll.load_credentials(None)
        with self.assertRaises(ValueError):
            enroll.validate_courses([{"type": "体育", "code": None}])

    def test_removed_batch_dialog_is_treated_as_closed(self):
        dialog = Mock(spec=WebElement, text="选择轮次")
        radio = Mock()
        button = Mock(text="确定")
        dialog.find_elements.side_effect = [[radio], [button]]
        dialog.is_displayed.side_effect = enroll.StaleElementReferenceException()
        self.mock("visible_elements", return_value=[dialog])
        self.assertTrue(enroll.confirm_available_batch(Mock()))

    def test_permission_error_requires_manual_action(self):
        self.mock("visible_elements", return_value=[Mock()])
        with self.assertRaisesRegex(enroll.ManualActionRequired, "无访问权限"):
            enroll.wait_until_ready(Mock(), "student", "secret", 300)

    def test_quota_rules_preserve_internal_and_external_distinction(self):
        driver = Mock()
        driver.execute_script.return_value = {"SFYX": "0"}
        row = Mock()
        row.find_elements.return_value = [Mock(text=text) for text in (
            "TEST001", "001", "课程", "30/10", "20/12", "选择"
        )]
        outside = enroll.capacity_status(driver, row, "培养方案外课程")
        sports = enroll.capacity_status(driver, row, "体育课")
        self.assertFalse(outside["available"])
        self.assertEqual(outside["quota_remaining"], 0)
        self.assertTrue(sports["available"])
        self.assertEqual(sports["quota_remaining"], 10)

    def test_blank_course_code_cell_is_skipped(self):
        row = Mock()
        row.find_elements.return_value = [Mock(text=""), Mock(text="001")]
        self.mock("wait_for_table")
        self.mock("WebDriverWait")
        self.mock("visible_elements", return_value=[row])
        self.assertEqual(enroll.search_courses(Mock(), "TEST001", None), [])

    def test_session_recovery_works_without_periodic_reload(self):
        driver = Mock(current_url=enroll.HOME_URL)
        self.mock("parse_args")
        self.mock("load_config", return_value=(
            [self.target], enroll.DEFAULT_SETTINGS | {"page_reload_every": 0}
        ))
        self.mock("load_credentials", return_value=("student", "secret"))
        self.mock("create_driver", return_value=driver)
        ready = self.mock("wait_until_ready")
        menu = self.mock("select_menu")
        self.mock("choose_target_once", return_value=True)
        self.stack.enter_context(patch.object(enroll.time, "sleep"))
        enroll.main()
        self.assertEqual(ready.call_count, 2)
        menu.assert_called_once_with(driver, self.target["menu_name"])
        driver.quit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
