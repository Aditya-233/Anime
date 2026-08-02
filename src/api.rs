use anyhow::{anyhow, Result};
use regex::Regex;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::sync::OnceLock;

const DEFAULT_BASE_URL: &str = "https://animepahe.pw";
const USER_AGENT: &str = "Mozilla/5.0 (X11; Linux x86_64; rv:153.0) Gecko/20100101 Firefox/153.0";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AnimeResult {
    pub title: String,
    pub session: String,
    pub anime_type: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Episode {
    pub episode: f64,
    pub session: String,
    pub title: Option<String>,
}

impl Episode {
    pub fn episode_str(&self) -> String {
        let ep = self.episode;
        if (ep.fract() - 0.0).abs() < f64::EPSILON {
            format!("{:02}", ep as u64)
        } else {
            format!("{:.1}", ep)
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StreamLink {
    pub quality: String,
    pub kwik_url: String,
    pub audio: String,
}

pub fn load_firefox_cookies() -> (HashMap<String, String>, HashMap<String, String>) {
    let mut ap_cookies = HashMap::new();
    let mut kwik_cookies = HashMap::new();

    let home = std::env::var("HOME").unwrap_or_default();
    let firefox_dir = PathBuf::from(home).join(".config/mozilla/firefox");
    let mut db_path = None;
    if let Ok(entries) = fs::read_dir(&firefox_dir) {
        for entry in entries.flatten() {
            let p = entry.path().join("cookies.sqlite");
            if p.exists() { db_path = Some(p); break; }
        }
    }

    if let Some(db) = db_path {
        let temp_db = std::env::temp_dir().join(format!("ap_cookies_{}.sqlite", std::process::id()));
        if fs::copy(&db, &temp_db).is_ok() {
            if let Ok(out) = Command::new("sqlite3")
                .arg("-separator").arg("|").arg(&temp_db)
                .arg("SELECT host, name, value FROM moz_cookies WHERE host LIKE '%animepahe%' OR host LIKE '%kwik%'")
                .output() {
                for line in String::from_utf8_lossy(&out.stdout).lines() {
                    let parts: Vec<&str> = line.split('|').collect();
                    if parts.len() >= 3 {
                        let host = parts[0].trim();
                        let name = parts[1].trim().to_string();
                        let value = parts[2].trim().trim_matches('\'').to_string();
                        if host.contains("animepahe") { ap_cookies.insert(name, value); }
                        else if host.contains("kwik") { kwik_cookies.insert(name, value); }
                    }
                }
            }
            let _ = fs::remove_file(&temp_db);
        }
    }

    if !ap_cookies.contains_key("__ddg2_") {
        ap_cookies.insert("__ddg2_".to_string(), "abcdefghijklmnop".to_string());
    }
    (ap_cookies, kwik_cookies)
}

#[derive(Clone)]
pub struct AnimePaheApi {
    client: Client,
    base_url: String,
}

impl AnimePaheApi {
    pub fn new() -> Result<Self> {
        let mut builder = Client::builder().danger_accept_invalid_certs(true).user_agent(USER_AGENT);
        if let Ok(addr) = "104.21.37.233:443".parse() {
            builder = builder.resolve("animepahe.pw", addr).resolve("animepahe.ru", addr).resolve("animepahe.com", addr);
        }
        let client = builder.build()?;
        let base_url = std::env::var("ANIMEPAHE_BASE_URL").unwrap_or_else(|_| DEFAULT_BASE_URL.to_string());
        Ok(Self { client, base_url })
    }

    pub fn base_url(&self) -> &str { &self.base_url }

    fn build_cookie_header(&self, is_kwik: bool) -> String {
        let (ap, kwik) = load_firefox_cookies();
        let map = if is_kwik { kwik } else { ap };
        map.into_iter().map(|(k, v)| format!("{}={}", k, v)).collect::<Vec<_>>().join("; ")
    }

    pub async fn fetch_text(&self, url: &str, referer: Option<&str>) -> Result<String> {
        let cookie_hdr = self.build_cookie_header(url.contains("kwik"));
        let mut req = self.client.get(url).header("Cookie", cookie_hdr).header("Accept", "*/*");
        if let Some(ref_url) = referer { req = req.header("Referer", ref_url); }
        else { req = req.header("Referer", format!("{}/", self.base_url)); }

        if let Ok(resp) = req.send().await {
            if resp.status().is_success() {
                if let Ok(text) = resp.text().await {
                    if !text.contains("Web Filter Violation") && !text.contains("Access Blocked") && !text.contains("Just a moment...") {
                        return Ok(text);
                    }
                }
            }
        }
        self.fetch_via_curl_cffi(url, referer)
    }

    fn fetch_via_curl_cffi(&self, url: &str, referer: Option<&str>) -> Result<String> {
        let py_script = format!(
            "import sys\nsys.path.insert(0, '/home/aditya/Downloads/AnimePahe')\nfrom animepahe_dl import AnimePaheClient\nc = AnimePaheClient()\nheaders = {{'Referer': '{}'}} if '{}' else {{}}\nresp = c.get('{}', headers=headers)\nif resp:\n    print(c._body(resp).decode('utf-8', errors='replace'))\nelse:\n    sys.exit(1)\n",
            referer.unwrap_or(""), referer.unwrap_or(""), url
        );
        let output = Command::new("/home/aditya/anime-dl-venv/bin/python").arg("-c").arg(&py_script).output()?;
        if output.status.success() { Ok(String::from_utf8_lossy(&output.stdout).to_string()) }
        else { Err(anyhow!("curl_cffi fetch failed for {}", url)) }
    }

    async fn get_json(&self, url: &str, referer: Option<&str>) -> Result<Value> {
        let text = self.fetch_text(url, referer).await?;
        serde_json::from_str(&text).map_err(|e| anyhow!("JSON error for {}: {}", url, e))
    }

    pub async fn search(&self, query: &str) -> Result<Vec<AnimeResult>> {
        let encoded: String = url::form_urlencoded::byte_serialize(query.as_bytes()).collect();
        let url = format!("{}/api?m=search&q={}", self.base_url, encoded);
        let resp = self.get_json(&url, Some(&format!("{}/", self.base_url))).await?;
        let mut results = Vec::new();
        if let Some(data) = resp.get("data").and_then(|d| d.as_array()) {
            for item in data {
                let title = item.get("title").and_then(|v| v.as_str()).unwrap_or("Unknown").to_string();
                let session = item.get("session").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let anime_type = item.get("type").and_then(|v| v.as_str()).map(|s| s.to_string());
                results.push(AnimeResult { title, session, anime_type });
            }
        }
        Ok(results)
    }

    pub async fn get_episodes(&self, anime_session: &str) -> Result<Vec<Episode>> {
        let mut all_eps = Vec::new();
        let mut page = 1;
        loop {
            let url = format!("{}/api?m=release&id={}&sort=episode_asc&page={}", self.base_url, anime_session, page);
            let resp = self.get_json(&url, Some(&format!("{}/", self.base_url))).await?;
            if let Some(eps) = resp.get("data").and_then(|d| d.as_array()) {
                if eps.is_empty() { break; }
                for item in eps {
                    let episode = item.get("episode").and_then(|v| v.as_f64()).unwrap_or(0.0);
                    let session = item.get("session").and_then(|v| v.as_str()).unwrap_or("").to_string();
                    let title = item.get("title").and_then(|v| v.as_str()).map(|s| s.to_string());
                    all_eps.push(Episode { episode, session, title });
                }
            } else { break; }
            let last_page = resp.get("last_page").and_then(|v| v.as_u64()).unwrap_or(1);
            if page >= last_page { break; }
            page += 1;
        }
        Ok(all_eps)
    }

    pub async fn get_stream_links(&self, anime_session: &str, ep_session: &str) -> Result<Vec<StreamLink>> {
        let play_url = format!("{}/play/{}/{}", self.base_url, anime_session, ep_session);
        let html = self.fetch_text(&play_url, Some(&format!("{}/", self.base_url))).await?;
        static TAG_RE: OnceLock<Regex> = OnceLock::new();
        let tag_re = TAG_RE.get_or_init(|| Regex::new(r#"(?i)<(?:a|button)[^>]+(?:href|data-src|data-url|src)=["']([^"']+)["'][^>]*>(.*?)</(?:a|button)>"#).unwrap());
        static QUALITY_RE: OnceLock<Regex> = OnceLock::new();
        let quality_re = QUALITY_RE.get_or_init(|| Regex::new(r"(\d{3,4}p)").unwrap());

        let mut links = Vec::new();
        for cap in tag_re.captures_iter(&html) {
            let kwik_url = cap.get(1).map_or("", |m| m.as_str());
            if !kwik_url.contains("kwik") { continue; }
            let label_raw = cap.get(2).map_or("", |m| m.as_str());
            let label = label_raw.replace("&nbsp;", " ").trim().to_string();
            let quality = quality_re.captures(&label).or_else(|| quality_re.captures(kwik_url)).map(|c| c[1].to_string()).unwrap_or_else(|| "unknown".to_string());
            let audio = if label.to_lowercase().contains("dub") { "dub".to_string() } else { "sub".to_string() };
            links.push(StreamLink { quality, kwik_url: kwik_url.to_string(), audio });
        }
        Ok(links)
    }

    pub async fn get_m3u8_url(&self, kwik_url: &str, play_referer: &str) -> Result<String> {
        let html = self.fetch_text(kwik_url, Some(play_referer)).await?;
        static M3U8_RE: OnceLock<Regex> = OnceLock::new();
        let m3u8_re = M3U8_RE.get_or_init(|| Regex::new(r#"(?i)https?://[^\s"'<>\\]+\.m3u8[^\s"'<>\\]*"#).unwrap());
        if let Some(mat) = m3u8_re.find(&html) { return Ok(mat.as_str().to_string()); }

        static EVAL_RE: OnceLock<Regex> = OnceLock::new();
        let eval_re = EVAL_RE.get_or_init(|| Regex::new(r#"(?s)\}\s*\('(.*?)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'(.*?)'"#).unwrap());
        for caps in eval_re.captures_iter(&html) {
            let p = &caps[1];
            let a: u32 = caps[2].parse().unwrap_or(10);
            let c: u32 = caps[3].parse().unwrap_or(0);
            let k: Vec<&str> = caps[4].split('|').collect();
            let decoded = unpack_js(p, a, c, &k);
            if let Some(mat) = m3u8_re.find(&decoded) {
                return Ok(mat.as_str().trim_end_matches('\\').trim_end_matches('\'').trim_end_matches('"').to_string());
            }
        }
        Err(anyhow!("Failed to extract m3u8 stream URL"))
    }
}

pub fn unpack_js(p: &str, a: u32, mut c: u32, k: &[&str]) -> String {
    let base_n = |mut num: u32, base: u32| -> String {
        let alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
        if num == 0 { return "0".to_string(); }
        let mut res = String::new();
        let alpha_chars: Vec<char> = alphabet.chars().collect();
        while num > 0 {
            let rem = (num % base) as usize;
            if rem < alpha_chars.len() { res.insert(0, alpha_chars[rem]); }
            num /= base;
        }
        res
    };
    let mut result = p.to_string();
    while c > 0 {
        c -= 1;
        if (c as usize) < k.len() && !k[c as usize].is_empty() {
            let token = base_n(c, a);
            if let Ok(re) = Regex::new(&format!(r"\b{}\b", regex::escape(&token))) {
                result = re.replace_all(&result, k[c as usize]).to_string();
            }
        }
    }
    result
}

pub fn pick_quality(links: &[StreamLink], pref: &str, audio_pref: &str) -> Option<StreamLink> {
    // Filter to preferred audio, fall back to all if none match
    let pool: Vec<StreamLink> = links.iter().filter(|l| l.audio.eq_ignore_ascii_case(audio_pref)).cloned().collect();
    let mut pool = if !pool.is_empty() { pool } else { links.to_vec() };
    if pool.is_empty() { return None; }

    // Sort by resolution number descending (1080 > 720 > 480 > 360)
    let res_num = |l: &StreamLink| -> u32 {
        l.quality.trim_end_matches('p').parse::<u32>().unwrap_or(0)
    };
    pool.sort_by(|a, b| res_num(b).cmp(&res_num(a)));

    // "best" → highest resolution
    if pref.eq_ignore_ascii_case("best") {
        return pool.into_iter().next();
    }
    // Exact match (e.g. "1080p")
    if let Some(found) = pool.iter().find(|l| l.quality.eq_ignore_ascii_case(pref)) {
        return Some(found.clone());
    }
    // Partial match (e.g. "1080" matches "1080p")
    let clean = pref.trim_end_matches('p');
    if let Some(found) = pool.iter().find(|l| l.quality.starts_with(clean)) {
        return Some(found.clone());
    }
    // Not found → return highest available
    pool.into_iter().next()
}

pub fn safe_name(name: &str) -> String {
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| Regex::new(r#"[\\/:*?"<>|]"#).unwrap());
    re.replace_all(name, "_").trim().to_string()
}

pub fn ep_filename(title: &str, ep_num: &str, quality: &str, ext: &str) -> String {
    format!("{} - E{} [{}].{}", safe_name(title), ep_num, quality, ext)
}
