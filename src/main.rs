mod api;
mod downloader;

use api::*;
use downloader::*;
use anyhow::Result;
use std::env;
use std::io::{self, Write};
use std::path::PathBuf;

#[tokio::main]
async fn main() -> Result<()> {
    // Optionally accept an initial query as a positional argument, nothing else.
    let args: Vec<String> = env::args().skip(1).collect();
    let initial_query = if args.is_empty() { None } else { Some(args.join(" ")) };
    run_interactive_fzf(initial_query).await
}

// ─── FZF helper ─────────────────────────────────────────────────────────────

fn run_fzf(input: &str, multi: bool, prompt: &str) -> Result<Vec<String>> {
    use std::process::{Command, Stdio};
    let mut child = Command::new("fzf")
        .arg(if multi { "-m" } else { "+m" })
        .arg("--reverse")
        .arg("--cycle")
        .arg("--prompt").arg(prompt)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()?;

    if let Some(mut stdin) = child.stdin.take() {
        stdin.write_all(input.as_bytes())?;
    }
    let output = child.wait_with_output()?;
    if output.status.success() {
        let lines = String::from_utf8_lossy(&output.stdout)
            .lines()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect();
        Ok(lines)
    } else {
        Ok(Vec::new())
    }
}

// ─── Interactive flow ────────────────────────────────────────────────────────

async fn run_interactive_fzf(initial_query: Option<String>) -> Result<()> {
    // Step 0: get query
    let query = match initial_query {
        Some(q) if !q.trim().is_empty() => q,
        _ => {
            print!("\x1b[1;36mEnter anime name: \x1b[0m");
            io::stdout().flush()?;
            let mut buf = String::new();
            io::stdin().read_line(&mut buf)?;
            buf.trim().to_string()
        }
    };
    if query.is_empty() {
        eprintln!("\x1b[1;31mNo query entered!\x1b[0m");
        return Ok(());
    }

    // Step 1: search → single-select fzf
    println!("\x1b[1;34mSearching for '{}'...\x1b[0m", query);
    let api = AnimePaheApi::new()?;
    let results = api.search(&query).await?;
    if results.is_empty() {
        eprintln!("\x1b[1;31mNo results found!\x1b[0m");
        return Ok(());
    }

    let mut search_input = String::new();
    for (i, r) in results.iter().enumerate() {
        search_input.push_str(&format!(
            "{}\t{}\t[{}]\n",
            i + 1,
            r.title,
            r.anime_type.as_deref().unwrap_or("-")
        ));
    }
    let chosen_result = run_fzf(&search_input, false, "Select anime (ENTER): ")?;
    if chosen_result.is_empty() { return Ok(()); }

    let idx: usize = chosen_result[0].split('\t').next().unwrap_or("1")
        .parse::<usize>().unwrap_or(1).saturating_sub(1);
    let anime = &results[idx.min(results.len() - 1)];
    println!("\x1b[1;33mSelected:\x1b[0m {}\n", anime.title);

    // Step 2: episodes → multi-select fzf
    println!("\x1b[1;36m[Multi-Select]\x1b[0m \x1b[1;33mTAB\x1b[0m to select, \x1b[1;33mENTER\x1b[0m to download.\n");
    let eps = api.get_episodes(&anime.session).await?;
    if eps.is_empty() {
        eprintln!("\x1b[1;31mNo episodes found!\x1b[0m");
        return Ok(());
    }

    let mut ep_input = String::new();
    for ep in &eps {
        ep_input.push_str(&format!(
            "Ep {}\t{}\n",
            ep.episode_str(),
            ep.title.as_deref().unwrap_or("")
        ));
    }
    let selected = run_fzf(&ep_input, true, "Select episodes [TAB=multi, ENTER=download]: ")?;
    if selected.is_empty() {
        eprintln!("\x1b[1;31mNo episodes selected!\x1b[0m");
        return Ok(());
    }

    // Step 3: download selected episodes with live progress bar
    println!("\n\x1b[1;35mProcessing...\x1b[0m");
    let out_dir = {
        let home = env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."));
        home.join("Downloads").join(safe_name(&anime.title))
    };
    let downloader = HlsDownloader::new(4);

    for line in &selected {
        let ep_str = line.split('\t').next().unwrap_or("")
            .trim_start_matches("Ep ").trim();
        let ep = match eps.iter().find(|e| e.episode_str() == ep_str) {
            Some(e) => e,
            None => continue,
        };

        let links = api.get_stream_links(&anime.session, &ep.session).await?;
        if links.is_empty() { continue; }
        let chosen = pick_quality(&links, "1080p", "sub").unwrap_or_else(|| links[0].clone());

        let play_url = format!("{}/play/{}/{}", api.base_url(), anime.session, ep.session);
        let m3u8_url = api.get_m3u8_url(&chosen.kwik_url, &play_url).await?;
        let fname = ep_filename(&anime.title, &ep.episode_str(), &chosen.quality, "mp4");
        let out_path = out_dir.join(&fname);

        if out_path.exists() {
            println!("\x1b[1;33m[SKIP]\x1b[0m Already exists: {}", fname);
            continue;
        }

        println!("\x1b[1;32m[INSTALL]\x1b[0m Installing '{}' -> {}...", anime.title, fname);

        // Progress callback: prints a live updating % line
        let label = fname.clone();
        let cb = move |done: u64, total: u64| {
            let pct = done * 100 / total.max(1);
            let filled = (pct / 5) as usize; // 20-char bar
            let bar: String = "█".repeat(filled) + &"░".repeat(20 - filled.min(20));
            print!("\r  \x1b[1;36m{bar}\x1b[0m {pct:3}%  ({done}/{total} segs)  {label}   ");
            let _ = io::stdout().flush();
        };

        match downloader.download(m3u8_url, out_path.clone(), chosen.kwik_url, cb).await {
            Ok(_) => {
                let size_mb = std::fs::metadata(&out_path)
                    .map(|m| m.len() as f64 / (1024.0 * 1024.0))
                    .unwrap_or(0.0);
                println!("\r\x1b[2K\x1b[1;32m[SUCCESS]\x1b[0m Installed {} ({:.1} MB)\n", fname, size_mb);
            }
            Err(e) => eprintln!("\r\x1b[2K\x1b[1;31m[FAILED]\x1b[0m {}: {}\n", fname, e),
        }
    }

    println!("\x1b[1;32mAll done!\x1b[0m");
    Ok(())
}
