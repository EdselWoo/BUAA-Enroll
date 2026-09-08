"""BUAA_BROWSER_TESTS=1 python -m unittest discover -s tests -v"""

import os
import unittest
from urllib.parse import quote

from selenium import webdriver
from selenium.webdriver.common.by import By

from buaa_enroll import (
    ElementClickInterceptedException, ManualActionRequired, click_choose_button,
)


@unittest.skipUnless(os.environ.get("BUAA_BROWSER_TESTS") == "1", "需显式启用本地 Chrome 测试")
class BrowserClickTests(unittest.TestCase):
    def test_floating_tool_and_unknown_overlay(self):
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1000,700")
        driver = webdriver.Chrome(options=options)
        self.addCleanup(driver.quit)
        # 临时浏览器会话，只加载本地 HTML，不读取凭据或访问选课网站。
        driver.get("data:text/html;charset=utf-8," + quote("""
            <button id="choose" style="position:fixed;left:500px;top:250px;width:100px;height:40px"
                onclick="window.clicks=(window.clicks||0)+1">选择</button>
            <div id="overlay" draggable="true" class="centre-btn item el-icon-s-fold"
                style="position:fixed;left:490px;top:240px;width:120px;height:60px;z-index:10;background:gray">
                <span style="display:block;width:100%;height:100%">折叠</span>
            </div>
        """))
        button = driver.find_element(By.ID, "choose")
        with self.assertRaises(ElementClickInterceptedException):
            button.click()
        self.assertIsNone(driver.execute_script("return window.clicks"))

        click_choose_button(driver, button)
        self.assertEqual(driver.execute_script("return window.clicks"), 1)
        self.assertEqual(driver.find_elements(By.ID, "overlay"), [])

        driver.execute_script("""
            document.body.insertAdjacentHTML('beforeend',
                '<div id="unknown" class="unknown-overlay" '
                + 'style="position:fixed;left:490px;top:240px;width:120px;height:60px;z-index:10;background:gray"></div>');
        """)
        with self.assertRaises(ManualActionRequired):
            click_choose_button(driver, button)
        self.assertEqual(driver.execute_script("return window.clicks"), 1)

        driver.execute_script("document.getElementById('unknown').remove();")
        click_choose_button(driver, button)
        self.assertEqual(driver.execute_script("return window.clicks"), 2)


if __name__ == "__main__":
    unittest.main()
