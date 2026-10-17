use std::path::Path;
use tower_http::services::ServeFile;

/// The injected document lives in an extensionless private temporary file.
/// Its HTTP type must describe the HTML, rather than the temporary filename:
/// WebKit cancels document navigation for application/octet-stream.
pub(super) fn html_file(path: &Path) -> ServeFile {
    ServeFile::new_with_mime(
        path,
        &"text/html; charset=utf-8".parse().expect("valid HTML MIME"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{http::StatusCode, Router};
    use tower_http::services::ServeDir;

    #[tokio::test]
    async fn injected_spa_documents_have_html_type_and_assets_keep_their_type() {
        let web = tempfile::tempdir().unwrap();
        std::fs::write(web.path().join("index.html"), "original index").unwrap();
        std::fs::write(web.path().join("app.js"), "window.appLoaded=true;").unwrap();
        std::fs::write(web.path().join("app.css"), "body{color:red}").unwrap();
        let injected = tempfile::NamedTempFile::new().unwrap();
        let html = "<!doctype html><html><body><script>window.__HG_DESKTOP__={};</script>你好</body></html>";
        std::fs::write(injected.path(), html).unwrap();
        let app = Router::new()
            .route_service("/", html_file(injected.path()))
            .route_service("/index.html", html_file(injected.path()))
            .fallback_service(ServeDir::new(web.path()).fallback(html_file(injected.path())));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let client = reqwest::Client::new();
        for path in ["/", "/index.html", "/chat/123", "/settings?tab=account"] {
            for head in [false, true] {
                let request = if head {
                    client.head(format!("{base}{path}"))
                } else {
                    client.get(format!("{base}{path}"))
                };
                let response = request.send().await.unwrap();
                assert_eq!(response.status(), StatusCode::OK, "{path}");
                assert_eq!(
                    response.headers()["content-type"],
                    "text/html; charset=utf-8",
                    "{path}"
                );
                assert_eq!(
                    response.text().await.unwrap(),
                    if head { "" } else { html },
                    "{path}"
                );
            }
        }
        for (path, expected) in [("/app.js", "javascript"), ("/app.css", "text/css")] {
            let response = client.get(format!("{base}{path}")).send().await.unwrap();
            assert_eq!(response.status(), StatusCode::OK);
            assert!(response.headers()["content-type"]
                .to_str()
                .unwrap()
                .contains(expected));
        }
        server.abort();
    }
}
