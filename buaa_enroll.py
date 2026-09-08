# SPDX-License-Identifier: MIT

import argparse
import json
import math
import re
import time
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


PROJECT_DIR = Path(__file__).resolve().parent
HOME_URL = "https://byxk.buaa.edu.cn/xsxk/profile/index.html"
DEFAULT_CONFIG_FILE = PROJECT_DIR / "config.json"
DEFAULT_CREDENTIALS_FILE = PROJECT_DIR / "credentials.json"
DEFAULT_PROFILE_DIR = PROJECT_DIR / ".chrome-profile"
DEFAULT_SETTINGS = {
    "retry_interval": 2,
    "page_reload_every": 10,
    "login_reminder_interval": 300,
    "result_timeout": 30,
    "keep_browser_open": False,
}

# 兼容旧脚本中的课程类型名称，也可以直接填写网站菜单名称。
COURSE_TYPE_TO_MENU = {
    "一般专业": "培养方案内课程",
    "核心专业": "培养方案内课程",
    "核心通识": "通识选修课",
    "一般通识": "通识选修课",
    "体育": "体育课",
    "班级课表推荐课程": "班级课表推荐课程",
    "培养方案内课程": "培养方案内课程",
    "培养方案外课程": "培养方案外课程",
    "重修课程": "重修课程",
    "英语课": "英语课",
    "体育课": "体育课",
    "通识选修课": "通识选修课",
    "科研课堂": "科研课堂",
    "全校课程查询": "全校课程查询",
}


class ManualActionRequired(RuntimeError):
    pass


class LoginAutomationUnavailable(RuntimeError):
    pass


def parse_args():
    parser = argparse.ArgumentParser(
        prog="BUAA Enroll",
        description="Automate course availability checks on the BUAA enrollment website.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_FILE,
        help="configuration file (default: config.json)",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS_FILE,
        help="SSO credentials file (default: credentials.json)",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=DEFAULT_PROFILE_DIR,
        help="Chrome profile directory (default: .chrome-profile)",
    )
    return parser.parse_args()


def create_driver(profile_dir):
    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir.expanduser().resolve()}")
    options.add_argument("--start-maximized")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(60)
    return driver


def visible_elements(driver, by, value):
    result = []
    for element in driver.find_elements(by, value):
        try:
            if element.is_displayed():
                result.append(element)
        except StaleElementReferenceException:
            pass
    return result


def read_json(path, label):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"未找到 {label}：{path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取 {label}：{error}") from error


def load_credentials(credentials_file):
    data = read_json(credentials_file, "credentials.json")
    if not isinstance(data, dict):
        raise ValueError("credentials.json 的顶层必须是 JSON 对象")
    username = data.get("username")
    password = data.get("password")
    if (
        not isinstance(username, str) or not username.strip()
        or not isinstance(password, str) or not password
    ):
        raise ValueError("credentials.json 中的 username 和 password 必须是非空字符串")
    return username.strip(), password


def try_sso_login(driver, username, password):
    if not username or not password or "sso.buaa.edu.cn/login" not in driver.current_url:
        return False

    frames = driver.find_elements(By.ID, "loginIframe")
    if not frames:
        return False

    driver.switch_to.frame(frames[0])
    try:
        if visible_elements(driver, By.ID, "captchaPasswor"):
            raise LoginAutomationUnavailable("SSO 要求验证码，无法自动登录")

        username_inputs = visible_elements(driver, By.ID, "unPassword")
        password_inputs = visible_elements(driver, By.ID, "pwPassword")
        submit_buttons = visible_elements(
            driver, By.CSS_SELECTOR, "input.submit-btn[value='登录']"
        )
        if not username_inputs or not password_inputs or not submit_buttons:
            return False

        username_inputs[0].clear()
        username_inputs[0].send_keys(username)
        password_inputs[0].clear()
        password_inputs[0].send_keys(password)
        submit_buttons[0].click()
        print("已使用本地凭据提交统一身份认证。")
        return True
    finally:
        driver.switch_to.default_content()


def confirm_available_batch(driver):
    """确认网站已经勾选的可用轮次，不选择不可用轮次。"""
    for dialog in visible_elements(driver, By.CSS_SELECTOR, ".el-dialog__wrapper"):
        if "选择轮次" not in dialog.text:
            continue

        checked = [
            item
            for item in dialog.find_elements(By.CSS_SELECTOR, "input[type='radio']")
            if item.is_selected() and item.is_enabled()
        ]
        if not checked:
            raise ManualActionRequired("网站没有自动勾选可用的选课轮次")

        confirm_buttons = [
            button
            for button in dialog.find_elements(By.TAG_NAME, "button")
            if button.is_displayed() and button.text.replace(" ", "").strip() == "确定"
        ]
        if not confirm_buttons:
            raise RuntimeError("选择轮次窗口中未找到确定按钮")

        confirm_buttons[0].click()
        WebDriverWait(driver, 20).until(EC.invisibility_of_element(dialog))
        print("已确认网站自动选中的可用轮次。")
        return True

    return False


def wait_until_ready(driver, username, password, login_reminder_interval):
    """使用配置凭据登录，并从首页进入当前选课轮次。"""
    try:
        driver.get(HOME_URL)
    except TimeoutException:
        print("首页加载超时，将在当前页面继续等待。")
    print("正在使用 credentials.json 完成统一身份认证。")

    next_reminder = time.monotonic() + login_reminder_interval
    clicked = False
    login_attempted = False
    while True:
        if visible_elements(
            driver, By.XPATH, "//*[contains(text(), '学生无权限访问该轮次数据')]"
        ):
            raise ManualActionRequired("当前轮次无访问权限，请确认选课轮次和登录状态")
        if "/elective/grablessons" in driver.current_url:
            WebDriverWait(driver, 30).until(
                lambda d: visible_elements(
                    d, By.XPATH, "//*[@role='menuitem' or contains(@class, 'el-menu-item')]"
                )
            )
            return

        confirm_available_batch(driver)

        if not clicked:
            buttons = visible_elements(
                driver, By.XPATH, "//button[normalize-space()='选课']"
            )
            if buttons:
                buttons[0].click()
                clicked = True

        if "sso.buaa.edu.cn/login" in driver.current_url and not login_attempted:
            login_attempted = try_sso_login(driver, username, password)

        if time.monotonic() >= next_reminder:
            print("仍在等待登录或进入选课页面，可按 Ctrl+C 停止脚本。")
            next_reminder = time.monotonic() + login_reminder_interval

        time.sleep(1)


def select_menu(driver, menu_name):
    xpath = (
        "//*[@role='menuitem' or contains(@class, 'el-menu-item')]"
        f"[normalize-space()='{menu_name}']"
    )

    def active_menu(d):
        return next(
            (
                element
                for element in visible_elements(d, By.XPATH, xpath)
                if "is-active" in element.get_attribute("class")
            ),
            False,
        )

    for attempt in range(1, 4):
        try:
            wait_for_table(driver)
            menu = WebDriverWait(driver, 20).until(
                lambda d: next(iter(visible_elements(d, By.XPATH, xpath)), False)
            )
            if not active_menu(driver):
                menu.click()
            WebDriverWait(driver, 10).until(active_menu)
            wait_for_table(driver)
            return
        except (StaleElementReferenceException, TimeoutException):
            if attempt < 3:
                print(f"切换到“{menu_name}”未生效，正在重试（{attempt}/3）……")
                time.sleep(1)

    raise TimeoutException(f"连续 3 次无法切换到“{menu_name}”")


def reload_page(driver, menu_name, username, password, login_reminder_interval):
    """完整刷新当前页面；登录失效时重新等待用户登录。"""
    print("完整刷新选课页面……")
    try:
        driver.refresh()
    except TimeoutException:
        print("网页刷新超时，将检查当前页面状态。")
    try:
        WebDriverWait(driver, 30).until(
            lambda d: "/elective/grablessons" in d.current_url
            and visible_elements(
                d, By.XPATH, "//*[@role='menuitem' or contains(@class, 'el-menu-item')]"
            )
        )
    except TimeoutException:
        print("当前登录状态可能已经失效，重新等待登录。")
        wait_until_ready(driver, username, password, login_reminder_interval)

    select_menu(driver, menu_name)


def wait_for_table(driver):
    WebDriverWait(driver, 20).until(
        lambda d: d.execute_script(
            "return window.grablessonsVue && grablessonsVue.pubParam "
            "&& !grablessonsVue.pubParam.isScrolling"
        )
        and not visible_elements(d, By.CSS_SELECTOR, ".el-loading-mask")
    )


def search_courses(driver, course_code, serial_code):
    wait_for_table(driver)
    keyword = WebDriverWait(driver, 20).until(
        lambda d: next(
            iter(
                visible_elements(
                    d,
                    By.XPATH,
                    "//input[@placeholder='请输入课程代码/课程名称']",
                )
            ),
            False,
        )
    )
    keyword.clear()
    keyword.send_keys(course_code)

    search_started = driver.execute_script(
        "if (!window.grablessonsVue) return false;"
        "grablessonsVue.searchCourse();"
        "return true;"
    )
    if not search_started:
        raise RuntimeError("未找到网站的课程搜索组件，页面结构可能已变化")
    wait_for_table(driver)

    rows = visible_elements(
        driver,
        By.XPATH,
        "//div[contains(@class, 'el-table__body-wrapper')]//tbody/tr[td]",
    )
    matches = []
    for row in rows:
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) < 2:
            continue

        row_code = cells[0].text.strip().partition("\n")[0]
        row_serial = cells[1].text.strip().zfill(3)
        if row_code == course_code and (
            serial_code is None or row_serial == serial_code
        ):
            matches.append(row)

    return matches


def course_identity(row):
    cells = row.find_elements(By.TAG_NAME, "td")
    return cells[0].text.strip().partition("\n")[0], cells[1].text.strip().zfill(3)


def capacity_status(driver, row, menu_name):
    """返回容量信息；体育课看对内名额，其他课程看对外名额。"""
    cells = row.find_elements(By.TAG_NAME, "td")
    if len(cells) < 4:
        return None

    course_code, serial_code = course_identity(row)
    flags = driver.execute_script(
        "if (!window.grablessonsVue) return null;"
        "const rows = [...(grablessonsVue.courseList || []), "
        "...(grablessonsVue.catchCourseList || [])];"
        "const course = rows.find("
        "item => item.KCH === arguments[0] && String(item.KXH).padStart(3, '0') === arguments[1]"
        ");"
        "return course ? {SFYX: course.SFYX} : null;",
        course_code,
        serial_code,
    )
    if flags is None:
        return None

    capacity_text = cells[-3].text.strip()
    selected_text = cells[-2].text.strip()
    capacity = [int(value) for value in re.findall(r"\d+", capacity_text)]
    selected = [int(value) for value in re.findall(r"\d+", selected_text)]
    if not capacity or len(capacity) != len(selected):
        return None

    remaining = [max(total - used, 0) for total, used in zip(capacity, selected)]
    quota_index = 0 if menu_name == "体育课" else -1
    quota_name = "对内" if quota_index == 0 else "对外"
    quota_remaining = remaining[quota_index]
    return {
        "capacity": capacity_text,
        "selected": selected_text,
        "remaining": "/".join(str(value) for value in remaining),
        "quota_name": quota_name,
        "quota_remaining": quota_remaining,
        "available": quota_remaining > 0,
        "selected_by_me": str(flags.get("SFYX", "")) == "1",
    }


def operation_text(row):
    buttons = row.find_elements(By.TAG_NAME, "button")
    return " ".join(button.text.strip() for button in buttons if button.is_displayed())


def confirm_normal_selection(driver):
    boxes = visible_elements(driver, By.CSS_SELECTOR, ".el-message-box__wrapper")
    if boxes:
        box = boxes[-1]
        message = box.find_element(By.CSS_SELECTOR, ".el-message-box__message").text.strip()
        if "确认选择课程吗" not in message:
            raise ManualActionRequired(f"网站要求额外确认：{message}")

        box.find_element(By.CSS_SELECTOR, "button.el-button--primary").click()
        WebDriverWait(driver, 10).until(
            lambda d: not visible_elements(d, By.CSS_SELECTOR, ".el-message-box__wrapper")
        )
        print("已自动确认选课。")
        return

    dialogs = visible_elements(driver, By.CSS_SELECTOR, ".el-dialog__wrapper")
    if dialogs:
        raise ManualActionRequired("该课程需要选择志愿、教材或实验班")

    raise RuntimeError("点击选择后没有出现预期的确认窗口")


def wait_for_result(driver, result_timeout):
    """监听明确结果；成功提示仍需核验课程状态，结果未知时暂停。"""
    deadline = time.monotonic() + result_timeout
    seen = set()
    queued = False

    while time.monotonic() < deadline:
        # 服务器可能返回冲突等二次确认，不自动越过这类限制。
        boxes = visible_elements(driver, By.CSS_SELECTOR, ".el-message-box__wrapper")
        if boxes:
            box = boxes[-1]
            message = box.text.strip().replace("\n", " ")
            if "确认选择课程吗" in message:
                print(f"网站二次确认：{message}")
                box.find_element(By.CSS_SELECTOR, "button.el-button--primary").click()
                WebDriverWait(driver, 10).until(
                    lambda d: not visible_elements(
                        d, By.CSS_SELECTOR, ".el-message-box__wrapper"
                    )
                )
                continue
            raise ManualActionRequired(f"网站要求额外确认：{message}")

        for element in visible_elements(driver, By.CSS_SELECTOR, ".el-message__content"):
            message = element.text.strip()
            if not message or message in seen:
                continue

            seen.add(message)
            print(f"网站提示：{message}")
            if "进入选课队列" in message:
                queued = True
            elif re.fullmatch(r"(?:选课|选择课程|操作)成功[！!。]?", message):
                return True
            elif any(word in message for word in ("失败", "未成功", "不成功", "已满", "冲突")):
                return False

        time.sleep(0.2)

    if queued:
        reason = "已进入队列，但未在限定时间内收到最终结果"
    else:
        reason = "未在限定时间内收到明确的选课结果"
    raise ManualActionRequired(f"{reason}；请核对选课结果，避免继续提交同类候选课程")


def choose_target_once(driver, target, result_timeout):
    course_code = target["course_code"]
    serial_code = target["serial_code"]
    target_label = course_code + (f"-{serial_code}" if serial_code else "")
    rows = search_courses(driver, course_code, serial_code)
    if not rows:
        print(f"未找到 {target_label}")
        return False

    for row in rows:
        try:
            row_code, row_serial = course_identity(row)
            row_label = f"{row_code}-{row_serial}"
            state = operation_text(row)
            if "退选" in state or "已选" in state:
                print(f"课程 {row_label} 已在选课结果中。")
                return True

            capacity = capacity_status(driver, row, target["menu_name"])
            if capacity is None:
                print(f"{row_label} 的容量格式无法识别，本次不提交。")
                continue

            if capacity["selected_by_me"]:
                print(f"课程 {row_label} 已在选课结果中。")
                return True

            print(
                f"{row_label}：容量 {capacity['capacity']}，"
                f"已选 {capacity['selected']}，剩余 {capacity['remaining']}，"
                f"{capacity['quota_name']}剩余 {capacity['quota_remaining']}"
            )
            if not capacity["available"]:
                print(f"{capacity['quota_name']}容量已满。")
                continue

            choose_buttons = [
                button
                for button in row.find_elements(By.TAG_NAME, "button")
                if button.is_displayed()
                and button.is_enabled()
                and button.text.strip() == "选择"
            ]
            if not choose_buttons:
                print(f"课程当前不可选择，操作栏显示：{state or '无按钮'}")
                continue

            print(f"{row_label} 有剩余容量，正在提交选择请求……")
        except StaleElementReferenceException:
            print("课程列表刚刚刷新，将重新查询。")
            return False

        try:
            choose_buttons[0].click()
            WebDriverWait(driver, 10).until(
                lambda d: visible_elements(d, By.CSS_SELECTOR, ".el-message-box__wrapper")
                or visible_elements(d, By.CSS_SELECTOR, ".el-dialog__wrapper")
            )
            confirm_normal_selection(driver)
            if not wait_for_result(driver, result_timeout):
                return False

            # 提示可能来自其他请求，只有目标教学班的已选状态能确认完成。
            for selected_row in search_courses(driver, row_code, row_serial):
                selected_state = operation_text(selected_row)
                if "退选" in selected_state or "已选" in selected_state:
                    return True
                selected_capacity = capacity_status(driver, selected_row, target["menu_name"])
                if selected_capacity and selected_capacity["selected_by_me"]:
                    return True
            raise ManualActionRequired(f"{row_label} 收到成功提示，但未查到已选状态，请核对结果")
        except (TimeoutException, StaleElementReferenceException) as error:
            raise ManualActionRequired(
                f"{row_label} 选择后的页面状态未能确认，请核对选课结果"
            ) from error

    print(f"{target_label} 当前没有多余容量。")
    return False


def validate_courses(courses):
    if not isinstance(courses, list) or not courses:
        raise ValueError("请至少配置一个目标课程")

    targets = []
    for index, course in enumerate(courses, 1):
        if not isinstance(course, dict):
            raise ValueError(f"courses 第 {index} 项必须是 JSON 对象")
        course_type = str(course.get("type", "")).strip()
        course_code = course.get("code")
        serial_value = course.get("serial")
        serial_code = str(serial_value).strip() if serial_value is not None else None
        course_name = str(course.get("name", "")).strip()
        if course_type not in COURSE_TYPE_TO_MENU:
            choices = "、".join(COURSE_TYPE_TO_MENU)
            raise ValueError(f"不支持的课程类型：{course_type}；可选值：{choices}")
        if not isinstance(course_code, str) or not course_code.strip():
            raise ValueError("目标课程代码必须是非空字符串")
        targets.append(
            {
                "menu_name": COURSE_TYPE_TO_MENU[course_type],
                "course_code": course_code.strip().upper(),
                "serial_code": serial_code.zfill(3) if serial_code else None,
                "course_name": course_name,
            }
        )
    return targets


def load_config(config_file):
    data = read_json(config_file, "config.json")
    if not isinstance(data, dict):
        raise ValueError("config.json 的顶层必须是 JSON 对象")

    targets = validate_courses(data.get("courses"))
    custom_settings = data.get("settings", {})
    if not isinstance(custom_settings, dict):
        raise ValueError("settings 必须是 JSON 对象")
    settings = DEFAULT_SETTINGS | custom_settings

    numeric_settings = (
        "retry_interval", "page_reload_every", "login_reminder_interval", "result_timeout"
    )
    try:
        for key in numeric_settings:
            if isinstance(settings[key], bool) or not math.isfinite(float(settings[key])):
                raise ValueError(f"{key} 必须是有限数值")
        if not float(settings["page_reload_every"]).is_integer():
            raise ValueError("page_reload_every 必须是整数")
        settings["retry_interval"] = float(settings["retry_interval"])
        settings["page_reload_every"] = int(float(settings["page_reload_every"]))
        settings["login_reminder_interval"] = float(
            settings["login_reminder_interval"]
        )
        settings["result_timeout"] = float(settings["result_timeout"])
    except (TypeError, ValueError) as error:
        raise ValueError("运行参数必须是有效数值") from error

    if settings["retry_interval"] < 0:
        raise ValueError("retry_interval 不能小于 0")
    if settings["page_reload_every"] < 0:
        raise ValueError("page_reload_every 不能小于 0")
    if settings["login_reminder_interval"] <= 0 or settings["result_timeout"] <= 0:
        raise ValueError("login_reminder_interval 和 result_timeout 必须大于 0")
    if not isinstance(settings["keep_browser_open"], bool):
        raise ValueError("keep_browser_open 必须是 true 或 false")

    return targets, settings


def main():
    args = parse_args()
    try:
        targets, settings = load_config(args.config.expanduser().resolve())
        username, password = load_credentials(args.credentials.expanduser().resolve())
    except ValueError as error:
        raise SystemExit(f"配置错误：{error}") from error

    target_groups = list(dict.fromkeys(target["menu_name"] for target in targets))

    driver = create_driver(args.profile_dir)
    try:
        wait_until_ready(
            driver,
            username,
            password,
            settings["login_reminder_interval"],
        )
        print(
            f"已配置 {len(targets)} 个候选课程，共 {len(target_groups)} 类；"
            "每类选中一门后停止该类。"
        )

        cycle = 0
        current_menu = None
        completed_groups = set()
        while True:
            if len(completed_groups) == len(target_groups):
                print("所有课程类别均已选中一门，脚本结束。")
                return

            cycle += 1
            print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 第 {cycle} 轮检查")
            unavailable_groups = set()
            for index, target in enumerate(targets, 1):
                if (
                    target["menu_name"] in completed_groups
                    or target["menu_name"] in unavailable_groups
                ):
                    continue

                if "/elective/grablessons" not in driver.current_url:
                    print("已离开选课页面，重新检查登录和当前轮次。")
                    wait_until_ready(
                        driver, username, password, settings["login_reminder_interval"]
                    )
                    current_menu = None

                if target["menu_name"] != current_menu:
                    try:
                        select_menu(driver, target["menu_name"])
                    except TimeoutException:
                        print(
                            f"“{target['menu_name']}”页面本轮未就绪，"
                            "跳过该类并在下一轮重试。"
                        )
                        unavailable_groups.add(target["menu_name"])
                        current_menu = None
                        continue
                    current_menu = target["menu_name"]

                name = f" {target['course_name']}" if target["course_name"] else ""
                serial = f"-{target['serial_code']}" if target["serial_code"] else ""
                print(
                    f"[{index}/{len(targets)}] {target['menu_name']} / "
                    f"{target['course_code']}{serial}{name}"
                )
                try:
                    success = choose_target_once(
                        driver, target, settings["result_timeout"]
                    )
                except (TimeoutException, StaleElementReferenceException):
                    print("查询超时或课程列表刚刚刷新，本次跳过并继续运行。")
                    success = False

                if success:
                    completed_groups.add(target["menu_name"])
                    print(
                        f"{target['menu_name']}已完成，"
                        "停止检查该类并继续其他类别。"
                    )
                time.sleep(settings["retry_interval"])

            if len(completed_groups) == len(target_groups):
                print("所有课程类别均已选中一门，脚本结束。")
                return

            if (
                settings["page_reload_every"] > 0
                and cycle % settings["page_reload_every"] == 0
            ):
                remaining_menu = next(
                    menu for menu in target_groups if menu not in completed_groups
                )
                try:
                    reload_page(
                        driver,
                        remaining_menu,
                        username,
                        password,
                        settings["login_reminder_interval"],
                    )
                    current_menu = remaining_menu
                except TimeoutException:
                    print("完整刷新后页面未及时就绪，跳过本次刷新并继续下一轮。")
                    current_menu = None
    except LoginAutomationUnavailable as error:
        print(f"\n{error}，程序已停止。")
    except ManualActionRequired as error:
        print(f"\n{error}")
        input("请在 Chrome 中手动处理；处理完成后按回车键退出脚本……")
    except KeyboardInterrupt:
        print("\n已停止运行。")
    finally:
        if settings["keep_browser_open"]:
            input("按回车键关闭 Chrome……")
        driver.quit()


if __name__ == "__main__":
    main()
