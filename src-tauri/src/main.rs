// Prevent additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // Contournement du crash WebKitGTK / Mesa 24-26 sous Linux (abort sur
    // DMA-BUF). Doit être positionné AVANT tout appel GTK / WebKitGTK.
    #[cfg(target_os = "linux")]
    {
        if std::env::var("WEBKIT_DISABLE_DMABUF_RENDERER").is_err() {
            std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
        }

        // Rétablissement des chemins de plugins GStreamer hôtes pour l'AppImage.
        // Les plugins existent sur le système hôte mais linuxdeploy ne les embarque
        // pas (ou en tronque un exemplaire), ce qui provoque "appsink not found" /
        // "autoaudiosink not found" dans l'environnement bac à sable de l'AppImage.
        let host_gst_paths = "/usr/lib/x86_64-linux-gnu/gstreamer-1.0:/usr/lib/gstreamer-1.0:/usr/lib64/gstreamer-1.0:/usr/local/lib/gstreamer-1.0";

        let new_system_path = match std::env::var("GST_PLUGIN_SYSTEM_PATH_1_0") {
            Ok(val) => format!("{}:{}", host_gst_paths, val),
            Err(_) => host_gst_paths.to_string(),
        };
        std::env::set_var("GST_PLUGIN_SYSTEM_PATH_1_0", new_system_path);

        let new_plugin_path = match std::env::var("GST_PLUGIN_PATH_1_0") {
            Ok(val) => format!("{}:{}", host_gst_paths, val),
            Err(_) => host_gst_paths.to_string(),
        };
        std::env::set_var("GST_PLUGIN_PATH_1_0", new_plugin_path);

        // Réinitialise le chemin du scanner GStreamer si besoin.
        if std::path::Path::new("/usr/lib/x86_64-linux-gnu/gstreamer1.0/gstreamer-1.0/gst-plugin-scanner").exists() {
            std::env::set_var("GST_PLUGIN_SCANNER_1_0", "/usr/lib/x86_64-linux-gnu/gstreamer1.0/gstreamer-1.0/gst-plugin-scanner");
        }
    }
    guitar_lab_lib::run();
}
