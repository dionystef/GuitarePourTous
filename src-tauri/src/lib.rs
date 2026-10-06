//! Wrapper desktop Tauri pour Guitar Lab — 100% autonome.
//!
//! Le moteur Python (main.py + torch/demucs/ffmpeg) est compilé en binaire
//! autonome (PyInstaller `guitarlab-engine`) et embarqué comme ressource Tauri.
//!
//! Au démarrage : le binaire est lancé en sous-processus, on attend que
//! `http://127.0.0.1:8005/api/health` réponde, puis la fenêtre charge le
//! backend. À la fermeture : le sous-processus est terminé (SIGTERM puis
//! SIGKILL). Aucun `expect`/`unwrap` bloquant : l'app ne crash jamais.

use std::{
    io::{BufRead, BufReader, Read, Write},
    net::TcpStream,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use tauri::{Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent};

const BACKEND_HOST: &str = "127.0.0.1";
const BACKEND_PORT: u16 = 8005;
const HEALTH_PATH: &str = "/api/health";
const HEALTHCHECK_TIMEOUT_SECS: u64 = 180;
const OFFLINE_PAGE: &str = "offline.html";

/// Racine du dépôt, résolue dynamiquement depuis le répertoire de compilation
/// (`src-tauri` → parent). Portable : ne dépend plus d'un chemin absolu figé
/// sur la machine de dev. En production ce chemin de dev n'existe pas, mais il
/// ne sert que de dernier recours (les ressources embarquées sont trouvées avant).
fn project_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Nom du binaire moteur selon la plateforme.
#[cfg(target_os = "windows")]
const ENGINE_NAME: &str = "guitarlab-engine.exe";
#[cfg(not(target_os = "windows"))]
const ENGINE_NAME: &str = "guitarlab-engine";

/// État partagé : poignée du sous-processus moteur (Arc pour les threads).
struct BackendState {
    child: Arc<Mutex<Option<Child>>>,
}

// --------------------------------------------------------------------------- //
// Chemins & healthcheck
// --------------------------------------------------------------------------- //
fn backend_url() -> String {
    format!("http://{BACKEND_HOST}:{BACKEND_PORT}/")
}

fn parse_backend_url() -> Option<Url> {
    backend_url().parse().ok()
}

fn backend_healthy() -> bool {
    let addr = format!("{BACKEND_HOST}:{BACKEND_PORT}");
    let Ok(mut stream) = TcpStream::connect(addr) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(800)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(800)));

    let request = format!(
        "GET {HEALTH_PATH} HTTP/1.1\r\nHost: {BACKEND_HOST}:{BACKEND_PORT}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 512];
    let n = stream.read(&mut buf).unwrap_or(0);
    let response = String::from_utf8_lossy(&buf[..n]);
    response.starts_with("HTTP/1.1 200") || response.contains("\"status\":\"ok\"")
}

// Collectionne un chemin et ses parents (jusqu'à depth niveaux).
fn with_parents(path: &Path, depth: usize) -> Vec<PathBuf> {
    let mut out = Vec::new();
    let mut cur = path.to_path_buf();
    for _ in 0..depth {
        out.push(cur.clone());
        match cur.parent() {
            Some(p) => cur = p.to_path_buf(),
            None => break,
        }
    }
    out
}

/// Localise le binaire moteur `guitarlab-engine` (ressources ou dépôt).
fn find_engine(app: &tauri::AppHandle) -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();

    // 1) Ressources embarquées (chemin relatif conservé) + variantes.
    if let Ok(res) = app.path().resource_dir() {
        for base in with_parents(&res, 3) {
            candidates.push(base.join("resources").join("engine").join(ENGINE_NAME));
            candidates.push(base.join("engine").join(ENGINE_NAME));
            candidates.push(base.join(ENGINE_NAME));
        }
    }
    // 2) GUITARLAB_HOME explicite.
    if let Ok(home) = std::env::var("GUITARLAB_HOME") {
        let base = PathBuf::from(home);
        candidates.push(base.join("src-tauri").join("resources").join("engine").join(ENGINE_NAME));
        candidates.push(base.join("resources").join("engine").join(ENGINE_NAME));
    }
    // 3) Relative à l'exécutable.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            for base in with_parents(dir, 4) {
                candidates.push(base.join("resources").join("engine").join(ENGINE_NAME));
                candidates.push(base.join("engine").join(ENGINE_NAME));
            }
        }
    }
    // 4) Dépôt de développement.
    candidates.push(project_root()
        .join("src-tauri").join("resources").join("engine").join(ENGINE_NAME));

    candidates.into_iter().find(|p| p.is_file())
}

/// Localise `ffmpeg` embarqué et retourne son répertoire (ajouté au PATH).
fn find_ffmpeg_dir(app: &tauri::AppHandle) -> Option<PathBuf> {
    #[cfg(target_os = "windows")]
    let names = ["ffmpeg.exe", "ffmpeg"];
    #[cfg(not(target_os = "windows"))]
    let names = ["ffmpeg"];

    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(res) = app.path().resource_dir() {
        for base in with_parents(&res, 3) {
            candidates.push(base.join("resources").join("bin"));
            candidates.push(base.join("bin"));
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            for base in with_parents(dir, 4) {
                candidates.push(base.join("resources").join("bin"));
                candidates.push(base.join("bin"));
            }
        }
    }
    candidates.push(project_root()
        .join("src-tauri").join("resources").join("bin"));

    candidates
        .into_iter()
        .find(|d| names.iter().any(|n| d.join(n).is_file()))
}

// --------------------------------------------------------------------------- //
// Accès sous-processus (sans panic)
// --------------------------------------------------------------------------- //
fn lock_backend(state: &BackendState) -> std::sync::MutexGuard<'_, Option<Child>> {
    match state.child.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    }
}

fn kill_backend(holder: &mut Option<Child>) {
    if let Some(mut child) = holder.take() {
        #[cfg(target_os = "windows")]
        {
            let pid = child.id().to_string();
            // Tue l'arbre complet de processus (Python + workers éventuels)
            let _ = Command::new("taskkill")
                .args(["/F", "/T", "/PID", &pid])
                .status();
            let _ = child.kill();
        }
        #[cfg(not(target_os = "windows"))]
        {
            let pid = child.id().to_string();
            let _ = Command::new("kill").args(["-TERM", &pid]).status();
            std::thread::sleep(Duration::from_millis(800));
            let _ = Command::new("kill").args(["-KILL", &pid]).status();
            let _ = child.kill();
        }
    }
}

// --------------------------------------------------------------------------- //
// Lancement du moteur autonome
// --------------------------------------------------------------------------- //
/// Lit une sortie (stdout/stderr) ligne à ligne, l'écrit dans `engine.log`
/// et l'injecte dans la page d'attente via `window.__addLog`.
fn pipe_logs(
    mut reader: impl BufRead,
    win: tauri::WebviewWindow,
    log: Arc<Mutex<Option<std::fs::File>>>,
) {
    let mut buf = String::new();
    loop {
        buf.clear();
        match reader.read_line(&mut buf) {
            Ok(0) => break,
            Ok(_) => {
                let line = buf.trim_end();
                if line.is_empty() {
                    continue;
                }
                if let Ok(mut guard) = log.lock() {
                    if let Some(ref mut f) = *guard {
                        let _ = writeln!(f, "{line}");
                    }
                }
                let js = serde_json::to_string(line).unwrap_or_default();
                let _ = win.eval(&format!("window.__addLog && window.__addLog({js});"));
            }
            Err(_) => break,
        }
    }
}

fn spawn_engine(app: &tauri::App, win: tauri::WebviewWindow) {
    if backend_healthy() {
        eprintln!("[guitarlab] Moteur déjà opérationnel sur {BACKEND_PORT}.");
        return;
    }

    let Some(engine) = find_engine(app.handle()) else {
        eprintln!(
            "[guitarlab] Moteur introuvable ({ENGINE_NAME}) dans les ressources. Impossible de démarrer."
        );
        return;
    };

    // Répertoire de travail où se trouvent les ressources (static/ via _MEIPASS
    // est géré par le moteur lui-même). On place DATA_DIR dans l'espace user.
    let data_dir = std::env::var("GUITARLAB_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            #[cfg(target_os = "windows")]
            {
                if let Ok(appdata) = std::env::var("LOCALAPPDATA") {
                    PathBuf::from(appdata).join("GuitarLab").join("data")
                } else if let Ok(userprofile) = std::env::var("USERPROFILE") {
                    PathBuf::from(userprofile).join(".guitarlab").join("data")
                } else {
                    PathBuf::from(".").join("data")
                }
            }
            #[cfg(not(target_os = "windows"))]
            {
                PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| ".".into()))
                    .join(".local").join("share").join("guitarlab").join("data")
            }
        });

    // Crée le dossier de données (contiendra engine.log).
    let _ = std::fs::create_dir_all(&data_dir);

    // Ajoute le dossier ffmpeg au PATH pour les sous-processus `ffmpeg`/`ffprobe`.
    let mut env_path = std::env::var("PATH").unwrap_or_default();
    if let Some(ffdir) = find_ffmpeg_dir(app.handle()) {
        #[cfg(target_os = "windows")]
        let sep = ";";
        #[cfg(not(target_os = "windows"))]
        let sep = ":";
        env_path = format!("{}{}{}", ffdir.display(), sep, env_path);
        eprintln!("[guitarlab] ffmpeg ajouté au PATH : {}", ffdir.display());
    }

    eprintln!("[guitarlab] Démarrage du moteur : {}", engine.display());

    let mut cmd = Command::new(&engine);
    cmd.env("HOST", BACKEND_HOST)
        .env("PORT", BACKEND_PORT.to_string())
        .env("DATA_DIR", &data_dir)
        .env("PATH", &env_path)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    match cmd.spawn() {
        Ok(mut child) => {
            let stdout = child.stdout.take();
            let stderr = child.stderr.take();

            {
                let state = app.state::<BackendState>();
                let mut guard = lock_backend(&state);
                *guard = Some(child);
            }

            // Journal partagé entre les threads stdout/stderr.
            let log_path = data_dir.join("engine.log");
            let log_file = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&log_path)
                .ok();
            let log: Arc<Mutex<Option<std::fs::File>>> = Arc::new(Mutex::new(log_file));

            if let Some(stdout) = stdout {
                let win_stdout = win.clone();
                let log_stdout = Arc::clone(&log);
                std::thread::spawn(move || {
                    pipe_logs(BufReader::new(stdout), win_stdout, log_stdout);
                });
            }
            if let Some(stderr) = stderr {
                let win_stderr = win.clone();
                let log_stderr = Arc::clone(&log);
                std::thread::spawn(move || {
                    pipe_logs(BufReader::new(stderr), win_stderr, log_stderr);
                });
            }

            // Surveille un arrêt prématuré du moteur (crash avant `backend_healthy`).
            let child_arc = app.state::<BackendState>().child.clone();
            let mon_win = win.clone();
            std::thread::spawn(move || loop {
                if backend_healthy() {
                    return;
                }
                let exited = {
                    let mut guard = match child_arc.lock() {
                        Ok(g) => g,
                        Err(p) => p.into_inner(),
                    };
                    match guard.as_mut() {
                        Some(child) => matches!(child.try_wait(), Ok(Some(_))),
                        None => return,
                    }
                };
                if exited {
                    let msg = "❌ Le moteur s'est arrêté inopinément (vérifiez engine.log).";
                    let js = serde_json::to_string(msg).unwrap_or_default();
                    let _ = mon_win.eval(&format!("window.__addLog && window.__addLog({js});"));
                    return;
                }
                std::thread::sleep(Duration::from_millis(500));
            });

            eprintln!("[guitarlab] Moteur lancé (data={})", data_dir.display());
        }
        Err(e) => {
            eprintln!("[guitarlab] Échec du lancement du moteur : {e}");
        }
    }
}

/// Surveillance : bascule vers la vraie interface dès que le moteur répond.
fn watch_backend(win: tauri::WebviewWindow) {
    let deadline = Instant::now() + Duration::from_secs(HEALTHCHECK_TIMEOUT_SECS);
    while Instant::now() < deadline {
        if backend_healthy() {
            eprintln!("[guitarlab] Moteur prêt — ouverture de {}", backend_url());
            if let Some(url) = parse_backend_url() {
                if let Err(e) = win.navigate(url) {
                    eprintln!("[guitarlab] Navigation vers le moteur impossible : {e}");
                }
            }
            return;
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    eprintln!("[guitarlab] Moteur non prêt après {HEALTHCHECK_TIMEOUT_SECS}s — page d'attente conservée.");
}

// --------------------------------------------------------------------------- //
// Purge du cache WebKit (évite les conflits de cache WebKitGTK / PWA sous Linux)
// --------------------------------------------------------------------------- //
#[cfg(not(target_os = "windows"))]
fn purge_webkit_cache() {
    if let Ok(home) = std::env::var("HOME") {
        let base = PathBuf::from(home);
        let webkit_cache = base.join(".local").join("share").join("com.guitarlab.desktop").join("WebKitCache");
        let general_cache = base.join(".cache").join("com.guitarlab.desktop");
        let _ = std::fs::remove_dir_all(webkit_cache);
        let _ = std::fs::remove_dir_all(general_cache);
    }
}

// --------------------------------------------------------------------------- //
// Bootstrap
// --------------------------------------------------------------------------- //
pub fn run() {
    let state = BackendState {
        child: Arc::new(Mutex::new(None)),
    };

    let app = tauri::Builder::default()
        .manage(state)
        .setup(|app| {
            // Purge le cache WebKitGTK au démarrage sous Linux (avant le moteur).
            #[cfg(not(target_os = "windows"))]
            purge_webkit_cache();

            // Fenêtre : on ouvre sur la page d'attente embarquée, puis on
            // bascule vers le moteur dès qu'il répond (évite l'erreur réseau).
            let win = WebviewWindowBuilder::new(
                app,
                "main",
                WebviewUrl::App(OFFLINE_PAGE.into()),
            )
            .title("Guitar Lab — Studio IA")
            .inner_size(1280.0, 820.0)
            .min_inner_size(960.0, 600.0)
            // DevTools uniquement en build debug (pas dans les builds de release).
            .devtools(cfg!(debug_assertions))
            .build()?;

            // Lance le moteur autonome (sans bloquer, sans paniquer) en
            // diffusant ses logs dans la page d'attente.
            spawn_engine(app, win.clone());

            let win_clone = win.clone();
            std::thread::spawn(move || watch_backend(win_clone));
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { .. } = event {
                let state = window.app_handle().state::<BackendState>();
                let mut guard = lock_backend(&state);
                kill_backend(&mut guard);
                drop(guard);
            }
        })
        .build(tauri::generate_context!());

    match app {
        Ok(app) => {
            app.run(|app_handle, event| {
                if let RunEvent::Exit = event {
                    let state = app_handle.state::<BackendState>();
                    let mut guard = lock_backend(&state);
                    kill_backend(&mut guard);
                    drop(guard);
                }
            });
        }
        Err(e) => eprintln!("[guitarlab] Échec d'initialisation Tauri : {e}"),
    }
}
