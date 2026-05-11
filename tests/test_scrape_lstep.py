import sys
import types
import unittest
from pathlib import Path


def install_selenium_stubs():
    selenium = types.ModuleType("selenium")
    webdriver = types.ModuleType("selenium.webdriver")
    common = types.ModuleType("selenium.common")
    exceptions = types.ModuleType("selenium.common.exceptions")
    chrome = types.ModuleType("selenium.webdriver.chrome")
    options_mod = types.ModuleType("selenium.webdriver.chrome.options")
    by_mod = types.ModuleType("selenium.webdriver.common.by")
    remote_webdriver = types.ModuleType("selenium.webdriver.remote.webdriver")
    remote_webelement = types.ModuleType("selenium.webdriver.remote.webelement")
    support_ui = types.ModuleType("selenium.webdriver.support.ui")

    class StaleElementReferenceException(Exception):
        pass

    class TimeoutException(Exception):
        pass

    class Options:
        def add_argument(self, *_args, **_kwargs):
            pass

    class By:
        CSS_SELECTOR = "css selector"
        XPATH = "xpath"

    class WebDriver:
        pass

    class WebElement:
        pass

    class WebDriverWait:
        def __init__(self, *_args, **_kwargs):
            pass

    exceptions.StaleElementReferenceException = StaleElementReferenceException
    exceptions.TimeoutException = TimeoutException
    options_mod.Options = Options
    by_mod.By = By
    remote_webdriver.WebDriver = WebDriver
    remote_webelement.WebElement = WebElement
    support_ui.WebDriverWait = WebDriverWait
    webdriver.Chrome = lambda *args, **kwargs: None

    for module in (
        selenium,
        webdriver,
        common,
        exceptions,
        chrome,
        options_mod,
        by_mod,
        remote_webdriver,
        remote_webelement,
        support_ui,
    ):
        sys.modules[module.__name__] = module


try:
    from selenium.webdriver.common.by import By
except ModuleNotFoundError:
    install_selenium_stubs()
    from selenium.webdriver.common.by import By

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import scrape_lstep


class FakeElement:
    def __init__(self, href="#", classes="", displayed=True, enabled=True):
        self.href = href
        self.classes = classes
        self.displayed = displayed
        self.enabled = enabled

    def is_displayed(self):
        return self.displayed

    def is_enabled(self):
        return self.enabled

    def get_attribute(self, name):
        values = {
            "href": self.href,
            "class": self.classes,
            "disabled": "",
            "aria-disabled": "",
        }
        return values.get(name, "")


class FakeDriver:
    def __init__(self):
        self.calls = []
        self.pager_next = FakeElement(href="https://manager.linestep.net/line/show?page=2")

    def find_elements(self, by, selector):
        self.calls.append((by, selector))
        if by == By.XPATH and "//nav[@aria-label='Pagination']" in selector:
            return [self.pager_next]
        return []


class PagerDetectionTest(unittest.TestCase):
    def test_find_next_button_limits_default_detection_to_pager_containers(self):
        driver = FakeDriver()

        result = scrape_lstep.find_next_button(
            driver,
            scrape_lstep.DEFAULT_NEXT_SELECTOR,
            current_page=1,
        )

        self.assertIs(result, driver.pager_next)
        xpath_calls = [selector for by, selector in driver.calls if by == By.XPATH]
        self.assertTrue(xpath_calls)
        self.assertFalse(any(selector.startswith("//a[") for selector in xpath_calls))

    def test_friend_list_url_guard_defaults_to_line_show(self):
        self.assertTrue(
            scrape_lstep.is_friend_list_url(
                "https://manager.linestep.net/line/show?page=2",
                scrape_lstep.DEFAULT_FRIEND_LIST_URL_KEYWORD,
            )
        )
        self.assertFalse(
            scrape_lstep.is_friend_list_url(
                "https://manager.linestep.net/line/detail/212548209",
                scrape_lstep.DEFAULT_FRIEND_LIST_URL_KEYWORD,
            )
        )


if __name__ == "__main__":
    unittest.main()
