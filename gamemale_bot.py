import requests
import re
import os
import sys
import time
import json
import hashlib
import logging
from http.cookies import SimpleCookie
from urllib.parse import urljoin, quote

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("GameMale")

BASE_URL = "https://www.gamemale.com"
DEBUG = os.environ.get("GM_DEBUG", "").lower() in ("1", "true", "yes")
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Referer": BASE_URL,
}


def _save_debug(name, content, is_binary=False):
    if not DEBUG:
        return
    os.makedirs(DEBUG_DIR, exist_ok=True)
    filepath = os.path.join(DEBUG_DIR, f"{name}_{int(time.time())}")
    try:
        if is_binary:
            with open(filepath, "wb") as f:
                f.write(content)
        else:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content if isinstance(content, str) else content.decode("utf-8", errors="replace"))
        logger.debug(f"调试文件已保存: {filepath}")
    except Exception as e:
        logger.debug(f"保存调试文件失败: {e}")


class GameMaleBot:
    def __init__(self, username=None, password=None, cookie_str=None):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.username = username
        self.password = password
        self.cookie_str = cookie_str
        self.formhash = None
        self.logged_in = False
        self.retry_count = 3
        self.retry_delay = 5

    def _request(self, method, url, **kwargs):
        for attempt in range(self.retry_count):
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
                return resp
            except requests.RequestException as e:
                logger.warning(f"请求失败 (尝试 {attempt + 1}/{self.retry_count}): {e}")
                if attempt < self.retry_count - 1:
                    time.sleep(self.retry_delay)
        logger.error(f"请求最终失败: {url}")
        return None

    def _extract_formhash(self, html):
        patterns = [
            r'name="formhash"\s+value="([a-f0-9]+)"',
            r'formhash=([a-f0-9]{8})',
            r'name="formhash"\s*value="([^"]+)"',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def _has_seccode(self, html):
        return bool(re.search(r'name="seccodeverify"|id="seccodeverify"|updateseccode\(|id="seccode_', html, re.IGNORECASE))

    def _extract_seccode_idhash(self, html):
        patterns = [
            r"updateseccode\(\s*'([^']+)'",
            r'id="seccode_([^"]+)"',
            r'seccode[_&]hash=([^"\'>\s&]+)',
            r'idhash["\s:=]+["\']?([^"\'>\s&]+)',
            r'name="seccodehash"\s+value="([^"]+)"',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def _update_formhash(self, html=None):
        if html:
            fh = self._extract_formhash(html)
            if fh:
                self.formhash = fh
                logger.info(f"formhash: {self.formhash}")
                return True
        resp = self._request("GET", BASE_URL)
        if resp:
            fh = self._extract_formhash(resp.text)
            if fh:
                self.formhash = fh
                logger.info(f"formhash: {self.formhash}")
                return True
        logger.warning("未能获取formhash")
        return False

    def login_by_cookie(self):
        if not self.cookie_str:
            logger.error("未提供Cookie")
            return False
        logger.info("使用Cookie登录...")
        cookie = SimpleCookie()
        try:
            cookie.load(self.cookie_str)
        except Exception as e:
            logger.error(f"Cookie解析失败: {e}")
            return False
        for key, morsel in cookie.items():
            self.session.cookies.set(key, morsel.value, domain=".gamemale.com")
        resp = self._request("GET", f"{BASE_URL}/forum.php")
        if not resp:
            logger.error("Cookie登录失败：无法访问网站")
            return False
        _save_debug("login_cookie_page", resp.text)
        html = resp.text
        uid_match = re.search(r'discuz_uid\s*=\s*[\'"](\d+)', html)
        if uid_match and uid_match.group(1) != "0":
            self.logged_in = True
            self._update_formhash(html)
            logger.info(f"Cookie登录成功 (uid={uid_match.group(1)})")
            return True
        if self._update_formhash(html):
            if self.formhash:
                self.logged_in = True
                logger.info("Cookie登录成功（formhash已获取）")
                return True
        logger.error("Cookie登录失败，Cookie可能已过期")
        return False

    def _login_submit(self, login_post_url, login_data, seccode_hash=None, max_captcha_retries=5):
        for captcha_attempt in range(max_captcha_retries + 1):
            resp = self._request("POST", login_post_url, data=login_data)
            if not resp:
                logger.error("登录请求失败")
                return False
            _save_debug(f"login_response_captcha{captcha_attempt}", resp.text)
            resp_text = resp.text
            if "欢迎" in resp_text or "succeed" in resp_text.lower() or "登录成功" in resp_text:
                logger.info("账号密码登录成功")
                self._update_formhash()
                self.logged_in = True
                return True
            if "密码错误" in resp_text or "密码不正确" in resp_text:
                logger.error("登录失败：密码错误")
                return False
            if "用户名" in resp_text and "不存在" in resp_text:
                logger.error("登录失败：用户名不存在")
                return False
            if "验证码" in resp_text or "seccode" in resp_text.lower():
                if captcha_attempt >= max_captcha_retries:
                    logger.error(f"验证码识别失败，已重试{max_captcha_retries}次")
                    return False
                logger.info(f"验证码错误，重新识别 (第{captcha_attempt + 1}次)...")
                if not seccode_hash:
                    seccode_hash = self._extract_seccodehash_from_login()
                if not seccode_hash:
                    logger.error("未找到验证码hash，无法重试")
                    return False
                seccode_url = f"{BASE_URL}/misc.php?mod=seccode&update={int(time.time())}&idhash={seccode_hash}"
                logger.info(f"验证码URL: {seccode_url}")
                seccode_resp = self._request("GET", seccode_url)
                if not seccode_resp or seccode_resp.status_code != 200:
                    logger.error("验证码图片下载失败")
                    continue
                _save_debug(f"seccode_login_{captcha_attempt}", seccode_resp.content, is_binary=True)
                seccode_text = self._ocr_image(seccode_resp.content)
                if not seccode_text:
                    logger.warning("验证码识别为空，重试...")
                    continue
                logger.info(f"验证码识别结果: {seccode_text}")
                check_url = f"{BASE_URL}/misc.php?mod=seccode&action=check&inajax=1&idhash={seccode_hash}&secverify={quote(seccode_text)}"
                check_resp = self._request("GET", check_url)
                if check_resp:
                    check_text = check_resp.text
                    _save_debug(f"seccode_check_{captcha_attempt}", check_text)
                    if "succeed" in check_text.lower():
                        logger.info("验证码预验证通过")
                    else:
                        logger.warning(f"验证码预验证未通过，仍尝试提交: {check_text[:200]}")
                login_data["seccodeverify"] = seccode_text
                login_data["seccodehash"] = seccode_hash
                continue
            break
        self._update_formhash()
        if self.formhash:
            check_resp = self._request("GET", f"{BASE_URL}/forum.php")
            if check_resp:
                uid_match = re.search(r'discuz_uid\s*=\s*[\'"](\d+)', check_resp.text)
                if uid_match and uid_match.group(1) != "0":
                    logger.info("账号密码登录成功")
                    self.logged_in = True
                    return True
        logger.error(f"登录失败，响应: {resp_text[:500]}")
        return False

    def _extract_seccodehash_from_login(self):
        resp = self._request("GET", f"{BASE_URL}/member.php?mod=logging&action=login", headers={"X-Requested-With": "XMLHttpRequest"})
        if not resp:
            return None
        html = resp.text
        hash_patterns = [
            r'operator="seccode"\s*[^>]*idhash="([^"]+)"',
            r'seccode[_&]hash=([^"\'>\s&]+)',
            r'idhash["\s:=]+["\']?([^"\'>\s&]+)',
            r'id="seccode_([^"]+)"',
            r'name="seccodehash"\s+value="([^"]+)"',
        ]
        for pattern in hash_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def _ocr_image(self, image_content):
        try:
            import ddddocr
            ocr = ddddocr.DdddOcr(show_ad=False)
            result = ocr.classification(image_content)
            return result.strip() if result else None
        except ImportError:
            pass
        try:
            import pytesseract
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(image_content))
            result = pytesseract.image_to_string(img, config="--psm 7 -c tessedit_char_whitelist=0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ").strip()
            return result if result else None
        except ImportError:
            logger.error("未安装OCR库，请安装: pip install ddddocr")
            return None

    def login_by_password(self):
        if not self.username or not self.password:
            logger.error("未提供用户名或密码")
            return False
        logger.info(f"使用账号密码登录: {self.username}")
        login_url = f"{BASE_URL}/member.php?mod=logging&action=login"
        resp = self._request("GET", login_url)
        if not resp:
            logger.error("无法访问登录页面")
            return False
        login_html = resp.text
        _save_debug("login_page", login_html)
        formhash = self._extract_formhash(login_html)
        if not formhash:
            logger.error("登录页面未找到formhash")
            return False
        loginhash = None
        loginhash_match = re.search(r'loginhash=([A-Za-z0-9]+)', login_html)
        if loginhash_match:
            loginhash = loginhash_match.group(1)
            logger.info(f"loginhash: {loginhash}")
        login_post_url = (
            f"{BASE_URL}/member.php?mod=logging&action=login&loginsubmit=yes"
            f"&infloat=yes&inajax=1"
        )
        if loginhash:
            login_post_url += f"&loginhash={loginhash}"
        password_md5 = hashlib.md5(self.password.encode("utf-8")).hexdigest()
        login_data = {
            "formhash": formhash,
            "username": self.username,
            "password": password_md5,
            "questionid": "0",
            "answer": "",
            "cookietime": "2592000",
        }
        seccode_hash = None
        if self._has_seccode(login_html):
            seccode_hash = self._extract_seccode_idhash(login_html)
            if seccode_hash:
                logger.info(f"登录页面包含验证码，idhash: {seccode_hash}")
                seccode_url = f"{BASE_URL}/misc.php?mod=seccode&update={int(time.time())}&idhash={seccode_hash}"
                seccode_resp = self._request("GET", seccode_url)
                if seccode_resp and seccode_resp.status_code == 200:
                    _save_debug("seccode_login_initial", seccode_resp.content, is_binary=True)
                    seccode_text = self._ocr_image(seccode_resp.content)
                    if seccode_text:
                        logger.info(f"验证码识别结果: {seccode_text}")
                        login_data["seccodeverify"] = seccode_text
                        login_data["seccodehash"] = seccode_hash
                    else:
                        logger.warning("验证码识别为空")
                else:
                    logger.warning("验证码图片下载失败")
            else:
                logger.warning("登录页面有验证码标记但未提取到idhash")
        return self._login_submit(login_post_url, login_data, seccode_hash=seccode_hash)

    def login(self):
        if self.cookie_str:
            if self.login_by_cookie():
                return True
            logger.warning("Cookie登录失败，尝试账号密码登录...")
        if self.username and self.password:
            return self.login_by_password()
        logger.error("没有可用的登录方式")
        return False

    def sign_k_misign(self):
        if not self.logged_in:
            logger.error("未登录，无法签到")
            return False
        logger.info("开始每日签到 (k_misign)...")
        index_resp = self._request("GET", f"{BASE_URL}/forum.php")
        if index_resp:
            index_html = index_resp.text
            _save_debug("forum_index", index_html)
            if re.search(r'class="[^"]*midaben_signpanel[^"]*visted[^"]*"', index_html) or \
               re.search(r'id="JD_sign"[^>]*class="[^"]*visted[^"]*"', index_html):
                consecutive_match = re.search(r'连续(\d+)天', index_html)
                consecutive = consecutive_match.group(1) if consecutive_match else "?"
                logger.info(f"今日已签到，连续{consecutive}天")
                return True
            if re.search(r'>已签到<', index_html):
                logger.info("今日已签到")
                return True
        sign_page_url = f"{BASE_URL}/k_misign-sign.html"
        resp = self._request("GET", sign_page_url)
        if not resp:
            sign_page_url = f"{BASE_URL}/plugin.php?id=k_misign:sign"
            resp = self._request("GET", sign_page_url)
        if not resp:
            logger.error("无法访问签到页面")
            return False
        html = resp.text
        _save_debug("sign_page", html)
        if re.search(r'已签到|已经签到|今日已签', html):
            logger.info("今日已签到，无需重复签到")
            return True
        formhash = self._extract_formhash(html) or self.formhash
        if not formhash:
            logger.error("签到页面未找到formhash")
            return False
        sign_ajax_url = (
            f"{BASE_URL}/plugin.php?id=k_misign:sign&operation=qiandao"
            f"&formhash={formhash}&inajax=1"
        )
        sign_data = {"formhash": formhash}
        resp = self._request("POST", sign_ajax_url, data=sign_data)
        if not resp:
            logger.error("签到请求失败")
            return False
        _save_debug("sign_response", resp.text)
        resp_text = resp.text
        if "签到成功" in resp_text or "succeed" in resp_text.lower() or "恭喜" in resp_text:
            logger.info("签到成功！")
            return True
        if re.search(r'已签到|已经签到', resp_text):
            logger.info("今日已签到")
            return True
        if "请先登录" in resp_text:
            logger.error("签到失败：未登录或登录已过期")
            self.logged_in = False
            return False
        logger.warning(f"签到结果未知，响应: {resp_text[:500]}")
        return "签到" in resp_text and "失败" not in resp_text

    def daily_card_it618(self):
        if not self.logged_in:
            logger.error("未登录，无法抽卡")
            return False
        logger.info("开始日常卡片抽奖 (it618_award)...")
        card_page_url = f"{BASE_URL}/it618_award-award.html"
        resp = self._request("GET", card_page_url)
        if not resp:
            logger.error("无法访问抽卡页面")
            return False
        html = resp.text
        _save_debug("card_page", html)
        if re.search(r'今日已抽|已经抽奖|次数已用完|本周已抽', html):
            logger.info("今日/本周已抽卡，无需重复抽卡")
            return True
        formhash = self._extract_formhash(html) or self.formhash
        if not formhash:
            logger.error("抽卡页面未找到formhash")
            return False
        getaward_url = (
            f"{BASE_URL}/plugin.php?id=it618_award:ajax"
            f"&ac=getaward&formhash={formhash}"
        )
        logger.info(f"抽卡请求: {getaward_url}")
        resp = self._request("GET", getaward_url,
                             headers={"X-Requested-With": "XMLHttpRequest", "Referer": card_page_url})
        if not resp:
            logger.error("抽卡请求失败")
            return False
        _save_debug("card_response", resp.text)
        return self._parse_card_result(resp.text)

    def _parse_card_result(self, resp_text):
        try:
            json_str = re.sub(r'^[^\{]*', '', resp_text)
            result = json.loads(json_str)
            tipname = result.get("tipname", "")
            tipvalue = result.get("tipvalue", "")
            if tipname == "ok":
                yes = result.get("yes", "")
                prize_name = yes.split("it618_split")[0] if yes else "未知奖品"
                logger.info(f"抽卡成功！获得: {prize_name}")
                if tipvalue:
                    logger.info(f"提示: {tipvalue}")
                return True
            elif tipname == "" and tipvalue is None:
                logger.info("今日已抽卡（服务端返回空结果）")
                return True
            else:
                logger.error(f"抽卡失败: {tipvalue}")
                if tipvalue and ("登录" in tipvalue or "请先" in tipvalue):
                    self.logged_in = False
                return False
        except (json.JSONDecodeError, ValueError):
            pass
        if re.search(r'恭喜|中奖|获得', resp_text):
            logger.info("抽卡成功！")
            return True
        if re.search(r'已抽|已经抽奖|次数|已用完', resp_text):
            logger.info("今日/本周已抽卡")
            return True
        if "请先登录" in resp_text:
            logger.error("抽卡失败：未登录或登录已过期")
            self.logged_in = False
            return False
        logger.warning(f"抽卡结果未知，响应: {resp_text[:500]}")
        return False

    def _try_ocr_seccode(self, html, source="discuz"):
        seccode_url = None
        if source == "it618":
            seccode_url = f"{BASE_URL}/plugin.php?id=it618_award:validatecode"
        else:
            seccode_hash = None
            hash_patterns = [
                r'seccode[_&]hash=([^"\'>\s&]+)',
                r'idhash["\s:=]+["\']?([^"\'>\s&]+)',
                r'id="seccode_([^"]+)"',
            ]
            for pattern in hash_patterns:
                match = re.search(pattern, html, re.IGNORECASE)
                if match:
                    seccode_hash = match.group(1)
                    break
            if seccode_hash:
                seccode_url = f"{BASE_URL}/misc.php?mod=seccode&update={int(time.time())}&idhash={seccode_hash}"
            else:
                url_match = re.search(r'(misc\.php\?mod=seccode[^"\'>\s]*)', html)
                if url_match:
                    seccode_url = urljoin(BASE_URL, url_match.group(1))
                else:
                    logger.error("未找到验证码URL")
                    return None
        logger.info(f"验证码URL: {seccode_url}")
        try:
            resp = self._request("GET", seccode_url)
            if not resp or resp.status_code != 200:
                logger.error(f"验证码下载失败: HTTP {resp.status_code if resp else 'N/A'}")
                return None
            _save_debug("seccode_image", resp.content, is_binary=True)
            try:
                import ddddocr
                ocr = ddddocr.DdddOcr()
                seccode = ocr.classification(resp.content)
                logger.info(f"验证码识别结果: {seccode}")
                return seccode
            except ImportError:
                logger.warning("未安装ddddocr，尝试pytesseract...")
                try:
                    import pytesseract
                    from PIL import Image
                    import io
                    img = Image.open(io.BytesIO(resp.content))
                    seccode = pytesseract.image_to_string(img, config="--psm 7").strip()
                    logger.info(f"验证码识别结果: {seccode}")
                    return seccode if seccode else None
                except ImportError:
                    logger.error("未安装OCR库（ddddocr或pytesseract），无法识别验证码")
                    return None
        except Exception as e:
            logger.error(f"验证码处理失败: {e}")
            return None

    def run(self):
        logger.info("=" * 50)
        logger.info("GameMale 每日自动任务开始")
        logger.info("=" * 50)
        if not self.login():
            logger.error("登录失败，任务终止")
            return False
        results = {}
        logger.info("-" * 30)
        results["签到"] = self.sign_k_misign()
        logger.info("-" * 30)
        time.sleep(2)
        results["日常卡片"] = self.daily_card_it618()
        logger.info("-" * 30)
        logger.info("任务执行结果汇总:")
        all_success = True
        for task, success in results.items():
            status = "成功" if success else "失败"
            logger.info(f"  {task}: {status}")
            if not success:
                all_success = False
        logger.info("=" * 50)
        return all_success


def _load_config():
    config_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.json"),
    ]
    for config_path in config_paths:
        if os.path.isfile(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                logger.info(f"已加载配置文件: {config_path}")
                return config
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"配置文件读取失败: {config_path} - {e}")
    return {}


def main():
    config = _load_config()
    username = os.environ.get("GM_USERNAME", "") or config.get("username", "")
    password = os.environ.get("GM_PASSWORD", "") or config.get("password", "")
    cookie_str = os.environ.get("GM_COOKIE", "") or config.get("cookie", "")
    debug = os.environ.get("GM_DEBUG", "").lower() in ("1", "true", "yes") or config.get("debug", False)
    if debug:
        global DEBUG
        DEBUG = True
        logger.setLevel(logging.DEBUG)
    if not username and not cookie_str:
        logger.error("请设置环境变量 GM_USERNAME 和 GM_PASSWORD，或 GM_COOKIE，或在 config.json 中配置")
        sys.exit(1)
    if not password and not cookie_str:
        logger.error("请设置环境变量 GM_PASSWORD 或 GM_COOKIE，或在 config.json 中配置")
        sys.exit(1)
    bot = GameMaleBot(
        username=username,
        password=password,
        cookie_str=cookie_str,
    )
    success = bot.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
