use aes::Aes128;
use aes::cipher::{Block, BlockDecryptMut, KeyIvInit};
use cbc::Decryptor;
use m3u8_rs::{MasterPlaylist, Playlist};
use reqwest::Client;
use std::fs::{self, File};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use tokio::sync::Semaphore;

type Aes128CbcDec = Decryptor<Aes128>;

pub struct HlsDownloader {
    client: Client,
    threads: usize,
}

impl HlsDownloader {
    pub fn new(threads: usize) -> Self {
        let client = Client::builder()
            .danger_accept_invalid_certs(true)
            .user_agent("Mozilla/5.0 (X11; Linux x86_64; rv:153.0) Gecko/20100101 Firefox/153.0")
            .build()
            .unwrap();
        Self { client, threads }
    }

    async fn fetch_bytes(&self, url: &str, referer: &str) -> anyhow::Result<Vec<u8>> {
        // m3u8 playlists and AES keys live on CDN domains that need TLS fingerprinting.
        // Always use curl_cffi for those. Only try plain reqwest for TS segments.
        let is_playlist_or_key = url.contains(".m3u8") || url.contains(".key") || url.contains("mon.key");
        if !is_playlist_or_key {
            // Fast path: plain reqwest for TS segments
            let (ap, kwik) = crate::api::load_firefox_cookies();
            let c_map = if url.contains("kwik") { kwik } else { ap };
            let c_str: String = c_map.into_iter().map(|(k, v)| format!("{}={}", k, v)).collect::<Vec<_>>().join("; ");

            if let Ok(resp) = self.client.get(url)
                .header("Referer", referer)
                .header("Cookie", c_str)
                .header("User-Agent", "Mozilla/5.0 (X11; Linux x86_64; rv:153.0) Gecko/20100101 Firefox/153.0")
                .send().await {
                if resp.status().is_success() {
                    if let Ok(bytes) = resp.bytes().await {
                        let b = bytes.to_vec();
                        // Accept if it looks like binary TS data (sync byte 0x47) or not HTML
                        if !b.starts_with(b"<!DOCTYPE") && !b.starts_with(b"<html") && !b.is_empty() {
                            return Ok(b);
                        }
                    }
                }
            }
        }
        // Fallback (or primary for playlists/keys): curl_cffi with Firefox TLS fingerprint
        self.fetch_bytes_via_curl_cffi(url, referer)
    }

    fn fetch_bytes_via_curl_cffi(&self, url: &str, referer: &str) -> anyhow::Result<Vec<u8>> {
        let py_script = format!(
            "import sys\nsys.path.insert(0, '/home/aditya/anime-dl-venv/lib/python3.14/site-packages')\nfrom curl_cffi.requests import Session\ns = Session(impersonate='firefox133', verify=False)\nresp = s.get('{}', headers={{'Referer': '{}', 'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0'}}, timeout=30)\nif resp and resp.status_code == 200:\n    sys.stdout.buffer.write(resp.content)\nelse:\n    sys.exit(1)\n",
            url, referer
        );
        let output = Command::new("/home/aditya/anime-dl-venv/bin/python").arg("-c").arg(&py_script).output()?;
        if output.status.success() && !output.stdout.is_empty() {
            Ok(output.stdout)
        } else {
            anyhow::bail!("curl_cffi fetch failed for {}: stderr={}", url, String::from_utf8_lossy(&output.stderr).chars().take(200).collect::<String>())
        }
    }

    /// Download an HLS stream to `out_path`.
    /// `progress_cb` is called with (done, total) after each segment completes.
    pub async fn download(
        &self,
        m3u8_url: String,
        out_path: PathBuf,
        referer: String,
        progress_cb: impl Fn(u64, u64) + Send + Sync + 'static,
    ) -> anyhow::Result<()> {
        let resp = self.fetch_bytes(&m3u8_url, &referer).await?;
        let media_playlist_url = match m3u8_rs::parse_playlist(&resp) {
            Ok((_, Playlist::MasterPlaylist(master))) => self.pick_highest_variant(&m3u8_url, &master)?,
            _ => m3u8_url.clone(),
        };

        let media_bytes = if media_playlist_url == m3u8_url {
            resp
        } else {
            self.fetch_bytes(&media_playlist_url, &referer).await?
        };

        let media_playlist = match m3u8_rs::parse_playlist(&media_bytes) {
            Ok((_, Playlist::MediaPlaylist(mp))) => mp,
            _ => anyhow::bail!("Failed to parse media playlist"),
        };

        let mut key_bytes: Option<Vec<u8>> = None;
        let mut iv_bytes: Option<Vec<u8>> = None;

        for seg in &media_playlist.segments {
            if let Some(ref key) = seg.key {
                if let Some(ref uri) = key.uri {
                    let key_url = if uri.starts_with("http") {
                        uri.clone()
                    } else {
                        url::Url::parse(&media_playlist_url)?.join(uri)?.to_string()
                    };
                    key_bytes = Some(self.fetch_bytes(&key_url, &referer).await?);
                    if let Some(ref iv) = key.iv {
                        if let Ok(b) = hex::decode(iv.trim_start_matches("0x")) {
                            iv_bytes = Some(b);
                        }
                    }
                    break;
                }
            }
        }

        let segments: Vec<String> = media_playlist.segments.iter().filter_map(|s| {
            if s.uri.starts_with("http") {
                Some(s.uri.clone())
            } else {
                url::Url::parse(&media_playlist_url)
                    .ok()
                    .and_then(|base| base.join(&s.uri).ok().map(|u| u.to_string()))
            }
        }).collect();

        if segments.is_empty() {
            anyhow::bail!("No video segments found in m3u8 playlist");
        }

        let total = segments.len() as u64;

        // Always start fresh: remove stale tmp dir so old segments don't poison the merge
        let tmp_dir = out_path.parent()
            .unwrap_or_else(|| Path::new("."))
            .join(format!(".tmp_{}", out_path.file_stem().unwrap_or_default().to_string_lossy()));
        let _ = fs::remove_dir_all(&tmp_dir);
        fs::create_dir_all(&tmp_dir)?;

        let client = Arc::new(self.client.clone());
        let referer = Arc::new(referer);
        let key_bytes = Arc::new(key_bytes);
        let iv_bytes = Arc::new(iv_bytes);
        let tmp_dir = Arc::new(tmp_dir);
        let semaphore = Arc::new(Semaphore::new(self.threads));
        let done_count = Arc::new(AtomicU64::new(0));
        let progress_cb = Arc::new(progress_cb);
        let mut tasks = Vec::new();

        for (idx, seg_url) in segments.into_iter().enumerate() {
            let sem = semaphore.clone();
            let client = client.clone();
            let referer = referer.clone();
            let key = key_bytes.clone();
            let iv = iv_bytes.clone();
            let tmp_dir = tmp_dir.clone();
            let done = done_count.clone();
            let cb = progress_cb.clone();

            tasks.push(tokio::spawn(async move {
                let _permit = sem.acquire().await.unwrap();
                let dst = tmp_dir.join(format!("seg_{:06}.ts", idx));

                let downloader = HlsDownloader { client: (*client).clone(), threads: 1 };
                let mut data = Vec::new();
                for _attempt in 0..3 {
                    if let Ok(d) = downloader.fetch_bytes(&seg_url, &referer).await {
                        if !d.is_empty() {
                            data = d;
                            break;
                        }
                    }
                    tokio::time::sleep(tokio::time::Duration::from_millis(300)).await;
                }

                if data.is_empty() {
                    anyhow::bail!("Failed to download segment {}", idx);
                }

                if let Some(ref k) = *key {
                    if k.len() >= 16 {
                        let iv_data = iv.as_ref().clone().unwrap_or_else(|| vec![0u8; 16]);
                        if iv_data.len() >= 16 {
                            let mut decryptor = Aes128CbcDec::new_from_slices(&k[..16], &iv_data[..16])
                                .map_err(|e| anyhow::anyhow!("AES init error: {e:?}"))?;
                            for chunk in data.chunks_exact_mut(16) {
                                decryptor.decrypt_block_mut(Block::<Aes128>::from_mut_slice(chunk));
                            }
                        }
                    }
                }

                fs::write(&dst, &data)?;

                let n = done.fetch_add(1, Ordering::Relaxed) + 1;
                cb(n, total);

                Ok::<usize, anyhow::Error>(data.len())
            }));
        }

        for task in tasks {
            let _ = task.await;
        }

        self.merge_segments(&tmp_dir, total, &out_path)?;
        let _ = fs::remove_dir_all(&*tmp_dir);
        Ok(())
    }

    fn pick_highest_variant(&self, base_url: &str, master: &MasterPlaylist) -> anyhow::Result<String> {
        let mut best_bw = 0u64;
        let mut best_uri = base_url.to_string();
        for var in &master.variants {
            if var.bandwidth > best_bw {
                best_bw = var.bandwidth;
                best_uri = if var.uri.starts_with("http") {
                    var.uri.clone()
                } else {
                    url::Url::parse(base_url)?.join(&var.uri)?.to_string()
                };
            }
        }
        Ok(best_uri)
    }

    fn merge_segments(&self, tmp_dir: &Path, count: u64, out_path: &Path) -> anyhow::Result<()> {
        if let Some(parent) = out_path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut out_file = File::create(out_path)?;
        for i in 0..count {
            let seg = tmp_dir.join(format!("seg_{:06}.ts", i));
            if seg.exists() {
                let mut in_file = File::open(&seg)?;
                std::io::copy(&mut in_file, &mut out_file)?;
            }
        }
        Ok(())
    }
}

mod hex {
    pub fn decode(hex: &str) -> Result<Vec<u8>, ()> {
        if hex.len() % 2 != 0 { return Err(()); }
        (0..hex.len()).step_by(2)
            .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).map_err(|_| ()))
            .collect()
    }
}
