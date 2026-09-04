import requests
import json
from dataclasses import dataclass
from time import monotonic
from urllib.parse import urlparse
from logger import Logger

TIME_CHECK_TIMEOUT = 5
COURSE_SELECTION_CONNECT_TIMEOUT = 0.75
COURSE_SELECTION_READ_TIMEOUT = 2.0
COURSE_SELECTION_TIMEOUT = (COURSE_SELECTION_CONNECT_TIMEOUT, COURSE_SELECTION_READ_TIMEOUT)
CONNECTION_RECENT_WINDOW = 1.0
CONNECTION_PROBE_TIMEOUT = 0.75
NETWORK_RETRY_DELAY = 1.0
SERVER_ERROR_RETRY_DELAY = 1.5
DEFAULT_RETRY_DELAY = 3.0
STATUS_CHECK_TIMEOUT = (0.75, 2.0)

@dataclass
class RetryDecision:
    delay: float | None = None
    reason: str | None = None
    stop_current_round: bool = False

class RequestManager:

    # The codes that indicate the operation was not successful but can be tried again.
    codes_to_try_again = [
        "VAL01",
        "VAL02",
        "VAL06",
        "VAL13",
        "VAL14",
        "VAL16",
        "ERRLoad",
        "NULLParam-CheckOgrenciKayitZamaniKontrolu",
        "Kontenjan Dolu",
    ]
    
    # Codes that indicate quota is full - should switch to backup CRN
    quota_full_codes = ["VAL06", "Kontenjan Dolu"]
    success_codes = ["successResult", "Ekleme İşlemi Başarılı", "Silme İşlemi Başarılı"]
    timeout_codes = ["VAL21"]
    
    # Source: https://github.com/MustafaKrc/ITU-CRN-Picker/blob/ffb2ca20c197092f54ade466439d890cd61acab6/core/crn_picker.py#L31
    return_values = {
        "successResult": "CRN {} için işlem başarıyla tamamlandı.",
        "errorResult": "CRN {} için Operasyon tamamlanamadı.",
        None: "CRN {} için Operasyon tamamlanamadı.",
        "error": "CRN {} için bir hata meydana geldi.",
        "VAL01": "CRN {} bir problemden dolayı alınamadı.",
        "VAL02": "CRN {} kayıt zaman engelinden dolayı alınamadı.",
        "VAL03": "CRN {} bu dönem zaten alındığından dolayı tekrar alınamadı.",
        "VAL04": "CRN {} ders planında yer almadığından dolayı alınamadı.",
        "VAL05": "CRN {} dönemlik maksimum kredi sınırını aştığından dolayı alınamadı.",
        "VAL06": "CRN {} kontenjan yetersizliğinden dolayı alınamadı.",
        "VAL07": "CRN {} daha önce AA notuyla verildiğinden dolayı alınamadı.",
        "VAL08": "CRN {} program şartını sağlamadığından dolayı alınamadı.",
        "VAL09": "CRN {} başka bir dersle çakıştığından dolayı alınamadı.",
        "VAL10": "CRN {} dersine kayıtlı olmadığınızdan dolayı hiç bir işlem yapılmadı.",
        "VAL11": "CRN {} önşartlardan dolayı alınamadı.",
        "VAL12": "CRN {} şu anki dönemde açılmadığından dolayı alınamadı.",
        "VAL13": "CRN {} geçici olarak engellenmiş olması sebebiyle alınamadı.",
        "VAL14": "Sistem geçici olarak yanıt vermiyor.",
        "VAL15": "Maksimum 12 CRN alabilirsiniz.",
        "VAL16": "Aktif bir işleminiz devam ettiğinden dolayı işlem yapılamadı.",
        "VAL18": "CRN {} engellendğinden dolayı alınamadı.",
        "VAL19": "CRN {} önlisans dersi olduğundan dolayı alınamadı.",
        "VAL20": "Dönem başına sadece 1 ders bırakabilirsiniz.",
        "CRNListEmpty": "CRN {} listesi boş göründüğünden alınamadı.",
        "CRNNotFound": "CRN {} bulunamadığından dolayı alınamadı.",
        "ERRLoad": "Sistem geçici olarak yanıt vermiyor.",
        "NULLParam-CheckOgrenciKayitZamaniKontrolu" : "CRN {} kayıt zaman engelinden dolayı alınamadı.",
        "Ekleme İşlemi Başarılı" : "CRN {} için ekleme işlemi başarıyla tamamlandı.",

        # Below are the codes that are not in the original source code.
        "Kontenjan Dolu" : "CRN {} için kontenjan dolu olduğundan dolayı alınamadı.",
        "Silme İşlemi Başarılı" : "CRN {} için silme işlemi başarıyla tamamlandı.",
        "VAL21": "İstek limitini aşıldığı için 1 saatlik ders seçim engeli yenildi.",
        "VAL22": "CRN {} daha önce CC ve üstü harf notu ile verildiği için yükseltmeye alınamaz."
    }

    def __init__(self, token, course_selection_url: str, course_time_check_url: str, backup_map: dict[str, str] = None) -> None:
        """
        Args:
            token: String token or callable token getter function
            course_selection_url: Course selection API URL
            course_time_check_url: Time check API URL
            backup_map: Dictionary mapping primary CRN to backup CRN
        """
        self._token = token
        self._token_getter = token if callable(token) else None
        self.course_selection_url = course_selection_url
        self.course_time_check_url = course_time_check_url
        self.backup_map = backup_map or {}
        self.original_backup_map = dict(self.backup_map)  # Keep a copy of the original backup map
        self._last_origin_response_at = None
        self._retry_counts = {}
        origin = urlparse(course_selection_url)
        self.api_origin = f"{origin.scheme}://{origin.netloc}"
        self.term_list_url = f"{self.api_origin}/api/ogrenci/DonemListesi"
        self.registered_courses_url_template = f"{self.api_origin}/api/ogrenci/sinif/KayitliSinifListesi/{{donem_id}}"
        self._current_term_id = None
        self.session = requests.Session()
        self.session.headers.update({
            'Connection': 'keep-alive',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36'
        })

    def _get_current_token(self) -> str:
        """Returns the current token."""
        if self._token_getter:
            return self._token_getter()
        return self._token

    def _get_headers(self) -> dict[str, str]:
        return {
            'Authorization': self._get_current_token(),
        }

    def _build_course_selection_payload(self, crn_list: list[str], scrn_list: list[str]) -> dict[str, list[str]]:
        return {
            "ECRN": crn_list,
            "SCRN": scrn_list
        }

    def _mark_origin_response(self) -> None:
        self._last_origin_response_at = monotonic()

    def has_recent_origin_response(self, max_idle_seconds: float = CONNECTION_RECENT_WINDOW) -> bool:
        if self._last_origin_response_at is None:
            return False

        return monotonic() - self._last_origin_response_at <= max_idle_seconds

    def keep_connection_ready_for_fallback(self) -> bool:
        """Does one short same-origin probe only if the pool has not seen a recent response."""
        if self.has_recent_origin_response():
            return True

        try:
            response = self.session.get(self.course_time_check_url, headers=self._get_headers(), timeout=CONNECTION_PROBE_TIMEOUT)
            _ = response.text
            self._mark_origin_response()
            Logger.log("Lokal fallback öncesi same-origin bağlantı kontrolü tamamlandı.", silent=True)
            return True
        except requests.RequestException as e:
            Logger.log(f"Lokal fallback öncesi same-origin bağlantı kontrolü başarısız: {e}", silent=True)
            return False

    def _parse_retry_after(self, retry_after: str | None) -> float | None:
        if retry_after is None:
            return None

        try:
            return max(float(retry_after), 0)
        except ValueError:
            return None

    def _retry_with_backoff(self, reason: str, base_delay: float, max_delay: float = DEFAULT_RETRY_DELAY) -> RetryDecision:
        retry_count = self._retry_counts.get(reason, 0)
        delay = min(base_delay * (2 ** retry_count), max_delay)
        self._retry_counts[reason] = retry_count + 1
        return RetryDecision(delay=delay, reason=reason)

    def _reset_retry_backoff(self) -> None:
        self._retry_counts.clear()

    def _iter_values(self, data):
        if isinstance(data, dict):
            yield data
            for value in data.values():
                yield from self._iter_values(value)
        elif isinstance(data, list):
            for item in data:
                yield from self._iter_values(item)

    def _extract_list(self, data) -> list:
        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, list):
                    return value

        return []

    def _extract_current_term_id(self, data) -> str | None:
        terms = self._extract_list(data)
        if not terms:
            return None

        preferred_terms = []
        for term in terms:
            if not isinstance(term, dict):
                continue

            is_current = any(
                term.get(key) is True
                for key in ["aktif", "aktifMi", "current", "isCurrent", "isActive"]
            )
            if is_current:
                preferred_terms.append(term)

        candidates = preferred_terms or [term for term in terms if isinstance(term, dict)]
        for term in candidates:
            for key in ["donemId", "donemID", "id", "ID", "anahtar", "donemAnahtari"]:
                if key in term and term[key] is not None:
                    return str(term[key])

        return None

    def _extract_registered_crns(self, data) -> set[str]:
        registered_crns = set()
        for item in self._iter_values(data):
            for key, value in item.items():
                if key.lower() == "crn" and value is not None:
                    registered_crns.add(str(value))

        return registered_crns

    def _get_current_term_id(self) -> str | None:
        if self._current_term_id is not None:
            return self._current_term_id

        try:
            response = self.session.get(self.term_list_url, headers=self._get_headers(), timeout=STATUS_CHECK_TIMEOUT)
        except requests.RequestException as e:
            Logger.log(f"Ders durum dogrulama donem listesi alinamadi: {e}", silent=True)
            return None

        self._mark_origin_response()

        if response.status_code != 200:
            self._log_http_error(response, "Ders durum dogrulama donem listesi")
            return None

        try:
            self._current_term_id = self._extract_current_term_id(json.loads(response.text))
        except Exception as e:
            Logger.log(f"Ders durum dogrulama donem listesi islenemedi: {e}", silent=True)
            return None

        return self._current_term_id

    def _get_registered_crns(self) -> set[str] | None:
        term_id = self._get_current_term_id()
        if term_id is None:
            return None

        try:
            response = self.session.get(self.registered_courses_url_template.format(donem_id=term_id), headers=self._get_headers(), timeout=STATUS_CHECK_TIMEOUT)
        except requests.RequestException as e:
            Logger.log(f"Ders durum dogrulama kayitli ders listesi alinamadi: {e}", silent=True)
            return None

        self._mark_origin_response()

        if response.status_code != 200:
            self._log_http_error(response, "Ders durum dogrulama kayitli ders listesi")
            return None

        try:
            return self._extract_registered_crns(json.loads(response.text))
        except Exception as e:
            Logger.log(f"Ders durum dogrulama kayitli ders listesi islenemedi: {e}", silent=True)
            return None

    def verify_uncertain_post_result(self, crn_list: list[str], scrn_list: list[str]) -> tuple[list[str], list[str], bool]:
        """Checks registered courses after an uncertain POST result to avoid duplicate retries."""
        registered_crns = self._get_registered_crns()
        if registered_crns is None:
            return crn_list, scrn_list, False

        verified_any = False
        remaining_crns = []
        for crn in crn_list:
            if crn in registered_crns:
                Logger.log(f"CRN {crn} durum kontrolunde kayitli gorundu; tekrar POST atilmayacak.")
                verified_any = True
            else:
                remaining_crns.append(crn)

        remaining_scrns = []
        for scrn in scrn_list:
            if scrn not in registered_crns:
                Logger.log(f"SCRN {scrn} durum kontrolunde kayitli gorunmedi; tekrar POST atilmayacak.")
                verified_any = True
            else:
                remaining_scrns.append(scrn)

        return remaining_crns, remaining_scrns, verified_any

    def _log_http_error(self, response: requests.Response, context: str) -> RetryDecision:
        """Builds a retry decision without creating long blocking sleeps."""
        status_code = response.status_code
        response_text = response.text[:500]

        if status_code == 401:
            Logger.log(f"{context}: HTTP 401 alindi. Token/oturum gecersiz olabilir, mevcut token yenilenene kadar tekrar denenecek.")
            return RetryDecision(delay=NETWORK_RETRY_DELAY, reason="auth")
        elif status_code == 403:
            Logger.log(f"{context}: HTTP 403 alindi. Oturumun bu islem icin yetkisi olmayabilir.")
            return RetryDecision(stop_current_round=True, reason="forbidden")
        elif status_code == 429:
            retry_after = response.headers.get("Retry-After")
            retry_delay = self._parse_retry_after(retry_after) or DEFAULT_RETRY_DELAY
            retry_after_text = f" Retry-After: {retry_after}." if retry_after else ""
            Logger.log(f"{context}: HTTP 429 alindi, istek limiti olabilir.{retry_after_text} Retry gecikmesi: {retry_delay} saniye.")
            return RetryDecision(delay=retry_delay, reason="rate_limit")
        elif 500 <= status_code < 600:
            Logger.log(f"{context}: HTTP {status_code} alindi. Sunucu tarafinda gecici hata olabilir.")
            return self._retry_with_backoff("server_error", SERVER_ERROR_RETRY_DELAY)
        else:
            Logger.log(f"{context}: HTTP {status_code} alindi. Yanit: {response_text}", silent=True)
            return RetryDecision(stop_current_round=True, reason="http_error")

        return RetryDecision()

    def close(self) -> None:
        self.session.close()

    def check_course_selection_time(self) -> bool | None:
        try:
            response = self.session.get(self.course_time_check_url, headers=self._get_headers(), timeout=TIME_CHECK_TIMEOUT)
        except requests.RequestException as e:
            Logger.log(f"Zaman kontrol request hatası: {e}", silent=True)
            return None
        Logger.log(f"Zaman kontrol request response mesajı: {response.text}", silent=True)

        self._mark_origin_response()

        if response.status_code != 200:
            self._log_http_error(response, "Zaman kontrol request")
            return None

        try:
            result_json = json.loads(response.text)
            enrollment_data = result_json["kayitZamanKontrolResult"]
            return enrollment_data["ogrenciSinifaKayitOlabilir"] or enrollment_data["ogrenciSiniftanAyrilabilir"]
        except Exception:
            return None

    def request_course_selection(self, crn_list: list[str], scrn_list: list[str]) -> tuple[list[str], list[str], RetryDecision]:
        # Send the request to the server.
        payload = self._build_course_selection_payload(crn_list, scrn_list)
        try:
            response = self.session.post(self.course_selection_url, headers=self._get_headers(), json=payload, timeout=COURSE_SELECTION_TIMEOUT)
        except requests.ConnectTimeout as e:
            Logger.log(f"Ders secim request connect timeout: {e}", silent=True)
            crn_list, scrn_list, verified = self.verify_uncertain_post_result(crn_list, scrn_list)
            if verified:
                return crn_list, scrn_list, RetryDecision()
            return crn_list, scrn_list, self._retry_with_backoff("connect_timeout", NETWORK_RETRY_DELAY)
        except requests.ReadTimeout as e:
            Logger.log(f"Ders secim request read timeout: {e}", silent=True)
            crn_list, scrn_list, verified = self.verify_uncertain_post_result(crn_list, scrn_list)
            if verified:
                return crn_list, scrn_list, RetryDecision()
            return crn_list, scrn_list, self._retry_with_backoff("read_timeout", NETWORK_RETRY_DELAY)
        except requests.Timeout as e:
            Logger.log(f"Ders secim request zaman asimi: {e}", silent=True)
            crn_list, scrn_list, verified = self.verify_uncertain_post_result(crn_list, scrn_list)
            if verified:
                return crn_list, scrn_list, RetryDecision()
            return crn_list, scrn_list, self._retry_with_backoff("timeout", NETWORK_RETRY_DELAY)
        except requests.RequestException as e:
            Logger.log(f"Ders secim request network hatasi: {e}", silent=True)
            crn_list, scrn_list, verified = self.verify_uncertain_post_result(crn_list, scrn_list)
            if verified:
                return crn_list, scrn_list, RetryDecision()
            return crn_list, scrn_list, self._retry_with_backoff("network_error", NETWORK_RETRY_DELAY)
        Logger.log(f"Ders Seçim request response mesajı: {response.text}", silent=True)
        
        self._mark_origin_response()

        if response.status_code != 200:
            retry_decision = self._log_http_error(response, "Ders secim request")
            return crn_list, scrn_list, retry_decision

        self._reset_retry_backoff()
        retry_decision = RetryDecision()
        try:
            result_json = json.loads(response.text)

            # Log the results of crn_list and determine if it is to be retried.
            for crn_result in result_json["ecrnResultList"]:
                crn = crn_result["crn"]
                result_code = crn_result["resultCode"]

                Logger.log(RequestManager.return_values.get(result_code, f"CRN {{}} için bilinmeyen hata kodu: {result_code}").format(crn))
                
                is_retriable = result_code in RequestManager.codes_to_try_again
                # Use the backup only if the quota is full, other codes in the codes_to_try_again array are usually caused by timing problems etc.
                should_use_backup = result_code in RequestManager.quota_full_codes
                is_success = result_code in RequestManager.success_codes
                is_limit_block = result_code in RequestManager.timeout_codes
                has_backup = crn in self.backup_map
                is_backup_crn = crn in self.original_backup_map.values()
            
                if is_limit_block:
                    Logger.log("VAL21 limit engeli algılandı; 1 saat uykuya geçilmeyecek, mevcut ders seçim turu durdurulacak.")
                    retry_decision = RetryDecision(stop_current_round=True, reason="val21_limit_block")
                    return crn_list, scrn_list, retry_decision
                elif is_success:
                    crn_list.remove(crn)
                elif is_retriable:
                    if result_code == "VAL16":
                        retry_decision = RetryDecision(delay=DEFAULT_RETRY_DELAY, reason="active_operation")
                    elif result_code in ["VAL14", "ERRLoad"]:
                        retry_decision = RetryDecision(delay=SERVER_ERROR_RETRY_DELAY, reason="temporary_server_result")
                    elif result_code in ["VAL01", "VAL02", "VAL13", "NULLParam-CheckOgrenciKayitZamaniKontrolu"]:
                        retry_decision = RetryDecision(delay=DEFAULT_RETRY_DELAY, reason="temporary_validation")

                    if should_use_backup and has_backup:
                        backup_crn = self.backup_map[crn]
                        if not is_backup_crn:
                            Logger.log(f"CRN {crn} yerine yedeği ({backup_crn}) denenecek...")
                        else:
                            Logger.log(f"Yedek CRN {crn} başarısız oldu, orijinal CRN ({backup_crn}) denenmeye devam edilecek...")

                        crn_list.remove(crn)
                        crn_list.append(backup_crn)

                        self.backup_map.pop(crn)
                        self.backup_map[backup_crn] = crn
                    else:
                        Logger.log(f"CRN {crn} tekrar denenecek...")
                else:
                    # Check if this CRN is currently a backup (swap happened before)
                    if is_backup_crn:
                        # Find the original CRN
                        original_crn = next((k for k, v in self.original_backup_map.items() if v == crn), None)
                        
                        if original_crn:
                            Logger.log(f"Yedek CRN {crn} başarısız oldu, orijinal CRN'ye ({original_crn}) dönülüyor...")
                            
                            crn_list.remove(crn)
                            crn_list.append(original_crn)

                            self.backup_map.pop(crn, None)
                        else:
                            crn_list.remove(crn)
                            
                    # Check if we have a backup to try
                    elif has_backup:
                        backup_crn = self.backup_map[crn]
                        Logger.log(f"CRN {crn} başarısız oldu, yedeği olan ({backup_crn}) denenecek...")
                        
                        crn_list.remove(crn)
                        crn_list.append(backup_crn)
                        
                        self.backup_map.pop(crn)
                        
                    # No backup available, just remove
                    else:
                        Logger.log(f"CRN {crn} listeden çıkarılıyor...")
                        crn_list.remove(crn)

            # Log the results of scrn_list and determine if it is to be retried.
            for scrn_result in result_json["scrnResultList"]:
                crn = scrn_result["crn"]
                result_code = scrn_result["resultCode"]

                Logger.log(RequestManager.return_values.get(result_code, f"CRN {{}} için bilinmeyen hata kodu: {result_code}").format(crn))
                if result_code in RequestManager.timeout_codes:
                    Logger.log("VAL21 limit engeli algılandı; 1 saat uykuya geçilmeyecek, mevcut ders seçim turu durdurulacak.")
                    retry_decision = RetryDecision(stop_current_round=True, reason="val21_limit_block")
                    return crn_list, scrn_list, retry_decision
                elif result_code in RequestManager.codes_to_try_again:
                    if result_code == "VAL16":
                        retry_decision = RetryDecision(delay=DEFAULT_RETRY_DELAY, reason="active_operation")
                    elif result_code in ["VAL14", "ERRLoad"]:
                        retry_decision = RetryDecision(delay=SERVER_ERROR_RETRY_DELAY, reason="temporary_server_result")
                    elif result_code in ["VAL01", "VAL02", "VAL13", "NULLParam-CheckOgrenciKayitZamaniKontrolu"]:
                        retry_decision = RetryDecision(delay=DEFAULT_RETRY_DELAY, reason="temporary_validation")
                    Logger.log(f"CRN {crn} tekrar denenecek...")
                else:
                    scrn_list.remove(crn)
        except json.JSONDecodeError as e:
            Logger.log(f"CRN listesi işlenirken JSON hatası meydana geldi, request geçerli bir JSON döndürmedi: {e}", silent=True)
            retry_decision = RetryDecision(delay=SERVER_ERROR_RETRY_DELAY, reason="invalid_json")
        except Exception as e:
            Logger.log(f"CRN listesi işlenirken bir hata meydana geldi: {e}", silent=True)
            retry_decision = RetryDecision(delay=SERVER_ERROR_RETRY_DELAY, reason="response_processing_error")
        finally:
            return crn_list, scrn_list, retry_decision
