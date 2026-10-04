// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Read the environment, build the service, serve it.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use napkin_host::FsConfig;
use napkin_web::auth::{Cognito, Local, Provider, Sessions};
use napkin_web::state::AppCtx;
use napkin_web::tenant::{Identity, Mode};

struct Settings {
    addr: SocketAddr,
    data_root: PathBuf,
    config_dir: PathBuf,
    static_dir: Option<PathBuf>,
    seed_dir: Option<PathBuf>,
    sandbox_origin: Option<String>,
    agent_cap: u32,
    secure_cookie: bool,
}

/// Who checks passwords, from the environment:
///   NAPKIN_AUTH=none     anonymous sessions (the default: tests and demos)
///   NAPKIN_AUTH=local    accounts in NAPKIN_AUTH_USERS (default <data>/accounts.json)
///   NAPKIN_AUTH=cognito  a Cognito user pool: NAPKIN_COGNITO_REGION, NAPKIN_COGNITO_CLIENT_ID
/// With accounts, NAPKIN_SESSION_SECRET (32+ bytes) signs the session cookies;
/// every server behind one load balancer gets the same one.
fn identity(data_root: &std::path::Path, secure: bool) -> Identity {
    let mode = env_string("NAPKIN_AUTH").unwrap_or_else(|| "none".into());
    if mode == "none" {
        return Identity::anonymous(secure);
    }
    let secret = env_string("NAPKIN_SESSION_SECRET").unwrap_or_default();
    let sessions = Sessions::new(secret.as_bytes()).unwrap_or_else(|e| panic!("{e}"));
    let provider = match mode.as_str() {
        "local" => Provider::Local(Local::new(local_users(data_root))),
        "cognito" => Provider::Cognito(Cognito::new(
            &env_string("NAPKIN_COGNITO_REGION").expect("NAPKIN_COGNITO_REGION is required with NAPKIN_AUTH=cognito"),
            &env_string("NAPKIN_COGNITO_CLIENT_ID").expect("NAPKIN_COGNITO_CLIENT_ID is required with NAPKIN_AUTH=cognito"),
        )),
        other => panic!("NAPKIN_AUTH must be none, local or cognito (got {other:?})"),
    };
    Identity { mode: Mode::Accounts { provider, sessions }, secure }
}

fn local_users(data_root: &std::path::Path) -> PathBuf {
    env_string("NAPKIN_AUTH_USERS").map(PathBuf::from).unwrap_or_else(|| data_root.join("accounts.json"))
}

/// `napkin-web add-user <user@agency> [--name "Full Name"]`: an account for
/// NAPKIN_AUTH=local, with a temporary password printed once; the first
/// sign-in asks for a new one. (On AWS, infra/scripts/create-users.sh makes
/// the same accounts in the Cognito pool.)
fn add_user(args: &[String], data_root: &std::path::Path) {
    let Some(username) = args.first() else {
        eprintln!("usage: napkin-web add-user <user@agency> [--name \"Full Name\"]");
        std::process::exit(2);
    };
    let name = args.iter().position(|a| a == "--name").and_then(|i| args.get(i + 1)).map(String::as_str);
    let temporary = format!("Napkin-{}", &uuid::Uuid::new_v4().simple().to_string()[..10]);
    let path = local_users(data_root);
    match Local::new(path.clone()).add(username, &temporary, name) {
        Ok(()) => println!("{username}\t{temporary}\t(temporary; changed at first sign-in) -> {}", path.display()),
        Err(e) => {
            eprintln!("{username}: {e}");
            std::process::exit(1);
        }
    }
}

fn env_string(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.trim().is_empty())
}

/// Where to listen.
///
/// `NAPKIN_WEB_ADDR` wins when set. Otherwise `PORT` — which every container
/// platform injects — means "you are in a container", so bind every interface
/// rather than loopback, where nothing outside the container could reach us.
fn listen_addr() -> SocketAddr {
    if let Some(addr) = env_string("NAPKIN_WEB_ADDR") {
        return addr.parse().expect("NAPKIN_WEB_ADDR must be host:port");
    }
    match env_string("PORT") {
        Some(port) => format!("0.0.0.0:{port}")
            .parse()
            .expect("PORT must be a port number"),
        None => "127.0.0.1:8080".parse().unwrap(),
    }
}

impl Settings {
    fn from_env() -> Self {
        let data_root = env_string("NAPKIN_WEB_DATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("napkin-web-data"));
        Self {
            addr: listen_addr(),
            config_dir: env_string("NAPKIN_CONFIG_DIR")
                .map(PathBuf::from)
                .unwrap_or_else(|| data_root.join("config")),
            static_dir: env_string("NAPKIN_WEB_STATIC")
                .map(PathBuf::from)
                .or_else(napkin_web::find_static),
            // Unset means "same origin as the shell". Setting it is what moves
            // app frames onto their own hostname; nothing else changes.
            sandbox_origin: env_string("NAPKIN_SANDBOX_ORIGIN"),
            seed_dir: env_string("NAPKIN_WEB_SEED").map(PathBuf::from),
            agent_cap: env_string("NAPKIN_AGENT_CAP")
                .and_then(|v| v.parse().ok())
                .unwrap_or(40),
            secure_cookie: env_string("NAPKIN_WEB_SECURE_COOKIE").is_some(),
            data_root,
        }
    }
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "napkin_web=info,tower_http=warn".into()),
        )
        .init();

    let settings = Settings::from_env();
    std::fs::create_dir_all(&settings.data_root).expect("cannot create the data directory");

    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("add-user") {
        add_user(&args[1..], &settings.data_root);
        return;
    }
    let identity = std::sync::Arc::new(identity(&settings.data_root, settings.secure_cookie));
    let auth = match &identity.mode {
        Mode::Anonymous => "anonymous",
        Mode::Accounts { provider: Provider::Local(_), .. } => "local accounts",
        Mode::Accounts { provider: Provider::Cognito(_), .. } => "cognito",
    };

    let ctx = Arc::new(
        AppCtx::new(
            settings.data_root.clone(),
            Arc::new(FsConfig::new(settings.config_dir.clone())),
            settings.sandbox_origin.clone(),
            settings.agent_cap,
        )
        .with_seed(settings.seed_dir.clone()),
    );

    let app = napkin_web::router_with(ctx, settings.static_dir.as_deref(), identity);

    let listener = tokio::net::TcpListener::bind(settings.addr)
        .await
        .unwrap_or_else(|e| panic!("cannot bind {}: {e}", settings.addr));

    tracing::info!(
        addr = %settings.addr,
        data = %settings.data_root.display(),
        shell = %settings.static_dir.as_ref().map(|p| p.display().to_string()).unwrap_or_else(|| "(none built)".into()),
        agent_cap = settings.agent_cap,
        auth,
        seed = %settings.seed_dir.as_ref().map(|p| p.display().to_string()).unwrap_or_else(|| "(none)".into()),
        "Napkin Studio OS — web",
    );

    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
            tracing::info!("shutting down");
        })
        .await
        .expect("server error");
}
