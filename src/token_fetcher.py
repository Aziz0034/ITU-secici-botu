# === IMPORTS ===
from driver_manager import DriverManager
from selenium.webdriver.common.by import By
from time import sleep, monotonic
from logger import Logger
import threading

# === CONSTANTS ===
PAGE_LOAD_DELAY = 3
TOKEN_URL = "https://obs.itu.edu.tr/api/ogrenci/Takvim/KayitZamaniKontrolu"
TOKEN_REFRESH_INTERVAL = 60  # Token refresh interval (seconds)

# === CLASS DEFINITON ===
class ContinuousTokenFetcher(threading.Thread):
    """
    Thread class that continuously fetches tokens in the background.
    Provides thread-safe token access.
    """
    def __init__(self, url: str, login: str, password: str, use_headless_browser: bool=False) -> None:
        super().__init__(daemon=True)
        self.url = url
        self.creds = [login, password]
        self.driver = None
        self._token = ""
        self._token_lock = threading.Lock()
        self._driver_lock = threading.RLock()
        self._running = False
        self._started_event = threading.Event()
        self.use_headless_browser = use_headless_browser

    def _is_login_page(self) -> bool:
        return self.driver is not None and "girisv3.itu.edu.tr" in self.driver.current_url

    def _clear_captured_requests(self) -> None:
        try:
            del self.driver.requests
        except Exception:
            pass

    def _set_token(self, token: str) -> bool:
        """Stores a token and returns whether this was the first successful token."""
        was_started = self._started_event.is_set()
        with self._token_lock:
            self._token = token

        if not was_started:
            self._started_event.set()

        return not was_started

    def login_to_kepler(self) -> None:
        """Starts the driver and performs login."""
        with self._driver_lock:
            is_repeat = self._started_event.is_set()

            Logger.log("Kepler aciliyor...", silent=is_repeat)
            if self.driver is None:
                self.driver = DriverManager.create_driver(headless=self.use_headless_browser)

            self.driver.get(self.url)

            # Wait to see if the URL changes to the login page.
            sleep(PAGE_LOAD_DELAY)
            if not self._is_login_page():
                Logger.log("Kepler'e giris yapilmis, giris yapma asamasi atlaniyor...", silent=is_repeat)
                return

            # Login to the system.
            Logger.log("Kepler'e giris yapiliyor...", silent=is_repeat)
            input_elements = self.driver.find_elements(By.TAG_NAME, "input")
            index = 0

            for element in input_elements:
                # Skip the hidden elements.
                if element.get_attribute("type") == "hidden":
                    continue

                element.click()
                if index <= 1:
                    element.send_keys(self.creds[index])
                index += 1
                sleep(.1)

            if "SelectIdentity" in self.driver.current_url:
                sleep(.1)
                Logger.log("Yatay gecis hesabi algilandi, aktif ITU hesabi secilecek.", silent=is_repeat)
                identity_cards = self.driver.find_elements(By.CLASS_NAME, "card-body")
                for identity_card in identity_cards:
                    rows = identity_card.find_elements(By.TAG_NAME, "tr")
                    last_row = rows[-1]
                    content = last_row.get_attribute("innerHTML").lower()
                    if "durum" in content and "aktif" in content:
                        try:
                            selected_field = rows[1].find_element(By.TAG_NAME, "td").get_attribute("innerHTML").strip()
                            selected_studentid = rows[2].find_element(By.TAG_NAME, "td").get_attribute("innerHTML").strip()
                            Logger.log(f"Secilen hesap: \"{selected_field} ({selected_studentid})\".")
                        except Exception:
                            pass

                        select_button = identity_card.find_element(By.TAG_NAME, "a")
                        select_button.click()
                        sleep(.1)
                        break

            Logger.log("Kepler'e giris yapildi, ders secim sitesine yonlendiriliyor...")
            self.driver.get(self.url)
            sleep(PAGE_LOAD_DELAY)

    def _fetch_token_once(self) -> str:
        """Single token fetch operation. Re-login if logged out after refresh."""
        with self._driver_lock:
            if self.driver is None:
                self.login_to_kepler()

            # If the url is not the target url, open the target url.
            if self.url not in self.driver.current_url:
                Logger.log("Ders secim sitesi aciliyor...", silent=self._started_event.is_set())
                self.driver.get(self.url)
                sleep(PAGE_LOAD_DELAY)

            self._clear_captured_requests()
            self.driver.refresh()
            sleep(1)

            # Check if we got logged out after refresh.
            if self._is_login_page():
                Logger.log("Kepler hesabindan cikildigi algilandi, tekrar giris yapiliyor...")
                self.login_to_kepler()
                self._clear_captured_requests()
                self.driver.refresh()
                sleep(1)

            for request in self.driver.requests:
                if request.response and TOKEN_URL in request.url:
                    token = request.headers.get("authorization") or request.headers.get("Authorization")
                    if token:
                        return token

        return ""

    def ensure_session_ready(self, timeout: float = 30) -> bool:
        """Refreshes the session and confirms that a usable token is ready before the critical window."""
        Logger.log("Kepler oturumu ve API Token hazirligi kontrol ediliyor...")
        deadline = monotonic() + timeout

        while monotonic() < deadline:
            try:
                new_token = self._fetch_token_once()
                if new_token and "ERROR" not in new_token:
                    self._set_token(new_token)
                    Logger.log("Kepler oturumu ve API Token hazir.")
                    return True
            except Exception as e:
                Logger.log(f"Kepler oturum hazirlik kontrolu hatasi: {e}", silent=True)

            sleep(1)

        return self.has_token()

    def run(self) -> None:
        """Thread main loop - continuously fetches tokens."""
        self._running = True
        Logger.log("Token fetcher thread baslatildi, surekli token alinacak...", silent=True)

        while self._running:
            try:
                Logger.log("Yeni API Token araniyor.", silent=True)
                new_token = self._fetch_token_once()
                if new_token and "ERROR" not in new_token and new_token != self._token:
                    is_first_token = self._set_token(new_token)
                    Logger.log("API Token guncellendi.")

                    if is_first_token:
                        Logger.log("Ilk token basariyla alindi.")
            except Exception as e:
                Logger.log(f"Token fetch hatasi: {e}", silent=True)

            sleep(TOKEN_REFRESH_INTERVAL)

    def get_token(self) -> str:
        """Thread-safe token access."""
        with self._token_lock:
            return self._token

    def wait_for_first_token(self, timeout: float = 60) -> bool:
        """Waits until the first token is received."""
        return self._started_event.wait(timeout)

    def stop(self) -> None:
        """Stops the token fetcher."""
        self._running = False
        Logger.log("Token fetcher thread durduruluyor...")
        if self.driver:
            try:
                self.driver.minimize_window()
            except Exception:
                pass

    def has_token(self) -> bool:
        """Checks if a token exists."""
        with self._token_lock:
            return len(self._token) > 0
