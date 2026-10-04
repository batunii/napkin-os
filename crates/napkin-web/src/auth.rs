// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Signing in.
//!
//! Every account is named `user@agency` (no dot after the agency): the part
//! after the `@` is the agency, whose workspace the person works in, and the
//! whole name is the person the decision chain records. Accounts are made by
//! an admin with a temporary password; the first sign-in asks the person to
//! choose their own (owner, 2026-10-04: invite only, no open sign-up).
//!
//! Who checks the password is a [`Provider`]:
//! - [`Cognito`] on AWS: the user pool holds the accounts and their passwords
//!   (USER_PASSWORD_AUTH, NEW_PASSWORD_REQUIRED on the first sign-in);
//! - [`Local`] for development and tests: a JSON file of accounts with argon2
//!   hashes, the same flow, no AWS.
//!
//! A signed-in person carries a session cookie: their account and its expiry,
//! signed with the server's secret (HMAC-SHA256). It is stateless, so any of
//! several servers behind a load balancer reads it, and nothing is stored.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use base64::engine::general_purpose::URL_SAFE_NO_PAD as B64;
use base64::Engine as _;
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;

pub const SESSION_COOKIE: &str = "napkin_session";
/// How long a sign-in lasts.
pub const SESSION_SECS: u64 = 7 * 24 * 3600;

/// A signed-in person: `engineer@napkin`, working in agency `napkin`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Account {
    pub username: String,
    pub agency: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
}

impl Account {
    /// `user@agency`, lower case, no dot after the agency: the only shape
    /// accepted. The agency names a workspace, so it is kept to letters,
    /// digits and hyphens.
    pub fn parse_username(raw: &str) -> Option<(String, String)> {
        let u = raw.trim().to_ascii_lowercase();
        let (user, agency) = u.split_once('@')?;
        let user_ok = !user.is_empty()
            && user.len() <= 64
            && user.chars().all(|c| c.is_ascii_alphanumeric() || "._-".contains(c));
        let agency_ok = !agency.is_empty()
            && agency.len() <= 40
            && agency.chars().all(|c| c.is_ascii_alphanumeric() || c == '-')
            && !agency.starts_with('-');
        (user_ok && agency_ok).then(|| (u.clone(), agency.to_string()))
    }
}

/// What a sign-in attempt comes to.
#[derive(Debug)]
pub enum SignIn {
    Done(Account),
    /// The first sign-in with a temporary password: the person chooses their
    /// own. `session` is what finishing it needs (Cognito's challenge session).
    NewPassword { session: String },
    Refused(String),
}

pub enum Provider {
    Local(Local),
    Cognito(Cognito),
}

impl Provider {
    pub async fn sign_in(&self, username: &str, password: &str) -> SignIn {
        let Some((username, _)) = Account::parse_username(username) else {
            return SignIn::Refused("Use your sign-in name, like name@agency.".into());
        };
        match self {
            Provider::Local(l) => l.sign_in(&username, password),
            Provider::Cognito(c) => c.sign_in(&username, password).await,
        }
    }

    pub async fn new_password(&self, username: &str, session: &str, new: &str) -> SignIn {
        let Some((username, _)) = Account::parse_username(username) else {
            return SignIn::Refused("Use your sign-in name, like name@agency.".into());
        };
        if let Some(why) = weak(new) {
            return SignIn::Refused(why.into());
        }
        match self {
            Provider::Local(l) => l.new_password(&username, session, new),
            Provider::Cognito(c) => c.new_password(&username, session, new).await,
        }
    }
}

/// The password rule, said the same way whoever checks it: at least 10
/// characters, with a letter and a number (the Cognito pool asks the same).
fn weak(p: &str) -> Option<&'static str> {
    if p.chars().count() < 10 {
        return Some("Choose a password of at least 10 characters.");
    }
    if !p.chars().any(|c| c.is_alphabetic()) || !p.chars().any(|c| c.is_ascii_digit()) {
        return Some("Use at least one letter and one number.");
    }
    None
}

// ── sessions ────────────────────────────────────────────────────────────────

/// Signs and reads session cookies.
pub struct Sessions {
    key: Vec<u8>,
}

#[derive(Serialize, Deserialize)]
struct Claims {
    #[serde(flatten)]
    account: Account,
    exp: u64,
}

fn now() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

impl Sessions {
    /// A secret of at least 32 bytes, or the server refuses to start with
    /// sign-in on: a short key is a forgeable session.
    pub fn new(secret: &[u8]) -> Result<Self, String> {
        if secret.len() < 32 {
            return Err("NAPKIN_SESSION_SECRET must be at least 32 bytes".into());
        }
        Ok(Self { key: secret.to_vec() })
    }

    fn mac(&self, body: &str) -> String {
        let mut m = Hmac::<Sha256>::new_from_slice(&self.key).expect("any key length");
        m.update(body.as_bytes());
        B64.encode(m.finalize().into_bytes())
    }

    pub fn issue(&self, account: &Account) -> String {
        let claims = Claims { account: account.clone(), exp: now() + SESSION_SECS };
        let body = B64.encode(serde_json::to_vec(&claims).unwrap_or_default());
        let sig = self.mac(&body);
        format!("{body}.{sig}")
    }

    /// The account a cookie names, when its signature holds and it has not run out.
    pub fn read(&self, cookie: &str) -> Option<Account> {
        let (body, sig) = cookie.split_once('.')?;
        let mut m = Hmac::<Sha256>::new_from_slice(&self.key).ok()?;
        m.update(body.as_bytes());
        m.verify_slice(&B64.decode(sig).ok()?).ok()?;
        let claims: Claims = serde_json::from_slice(&B64.decode(body).ok()?).ok()?;
        (claims.exp > now()).then_some(claims.account)
    }
}

// ── local accounts ──────────────────────────────────────────────────────────

/// Accounts in a JSON file: `{ "engineer@napkin": { name, hash, must_change } }`.
/// For development and tests; the same flow as Cognito.
pub struct Local {
    path: PathBuf,
    lock: Mutex<()>,
}

#[derive(Clone, Serialize, Deserialize)]
struct LocalUser {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    name: Option<String>,
    hash: String,
    /// A temporary password an admin set: the first sign-in changes it.
    #[serde(default)]
    must_change: bool,
    /// The one-time token a sign-in with the temporary password hands out,
    /// which choosing the new password must give back.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    challenge: Option<String>,
}

fn hash(password: &str) -> String {
    use argon2::password_hash::{rand_core::OsRng, PasswordHasher, SaltString};
    let salt = SaltString::generate(&mut OsRng);
    argon2::Argon2::default()
        .hash_password(password.as_bytes(), &salt)
        .map(|h| h.to_string())
        .unwrap_or_default()
}

fn verify(password: &str, stored: &str) -> bool {
    use argon2::password_hash::{PasswordHash, PasswordVerifier};
    PasswordHash::new(stored)
        .map(|h| argon2::Argon2::default().verify_password(password.as_bytes(), &h).is_ok())
        .unwrap_or(false)
}

impl Local {
    pub fn new(path: PathBuf) -> Self {
        Self { path, lock: Mutex::new(()) }
    }

    fn load(&self) -> BTreeMap<String, LocalUser> {
        std::fs::read(&self.path)
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default()
    }

    fn save(&self, users: &BTreeMap<String, LocalUser>) -> std::io::Result<()> {
        if let Some(dir) = self.path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let tmp = self.path.with_extension("json.tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(users)?)?;
        std::fs::rename(tmp, &self.path)
    }

    /// An admin adds an account with a temporary password (the first sign-in
    /// changes it). Refuses a name that is not `user@agency`.
    pub fn add(&self, username: &str, temporary: &str, name: Option<&str>) -> Result<(), String> {
        let (u, _) = Account::parse_username(username).ok_or("the name must be user@agency")?;
        let _g = self.lock.lock().unwrap();
        let mut users = self.load();
        users.insert(u, LocalUser { name: name.map(String::from), hash: hash(temporary), must_change: true, challenge: None });
        self.save(&users).map_err(|e| e.to_string())
    }

    fn account(username: &str, user: &LocalUser) -> Account {
        let (u, agency) = Account::parse_username(username).unwrap_or_default();
        Account { username: u, agency, name: user.name.clone() }
    }

    fn sign_in(&self, username: &str, password: &str) -> SignIn {
        let _g = self.lock.lock().unwrap();
        let mut users = self.load();
        match users.get_mut(username) {
            Some(user) if verify(password, &user.hash) => {
                if user.must_change {
                    // proof the temporary password was given, good for one change
                    let token = uuid::Uuid::new_v4().to_string();
                    user.challenge = Some(token.clone());
                    match self.save(&users) {
                        Ok(()) => SignIn::NewPassword { session: token },
                        Err(e) => SignIn::Refused(format!("Sign-in could not be saved ({e}).")),
                    }
                } else {
                    SignIn::Done(Self::account(username, user))
                }
            }
            _ => SignIn::Refused("That name and password do not match.".into()),
        }
    }

    fn new_password(&self, username: &str, session: &str, new: &str) -> SignIn {
        let _g = self.lock.lock().unwrap();
        let mut users = self.load();
        let Some(user) = users.get_mut(username) else {
            return SignIn::Refused("That name and password do not match.".into());
        };
        if !user.must_change || user.challenge.as_deref() != Some(session) {
            return SignIn::Refused("Sign in again with your temporary password.".into());
        }
        user.hash = hash(new);
        user.must_change = false;
        user.challenge = None;
        let account = Self::account(username, user);
        match self.save(&users) {
            Ok(()) => SignIn::Done(account),
            Err(e) => SignIn::Refused(format!("The new password could not be saved ({e}).")),
        }
    }
}

// ── Amazon Cognito ──────────────────────────────────────────────────────────

/// A Cognito user pool's app client (no client secret, USER_PASSWORD_AUTH).
/// The pool is the authority for the accounts and their passwords; the
/// tokens it answers with come straight from it over TLS, so their claims
/// are read, not re-verified.
pub struct Cognito {
    endpoint: String,
    client_id: String,
    http: reqwest::Client,
}

impl Cognito {
    pub fn new(region: &str, client_id: &str) -> Self {
        Self {
            endpoint: format!("https://cognito-idp.{region}.amazonaws.com/"),
            client_id: client_id.to_string(),
            http: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(15))
                .build()
                .unwrap_or_default(),
        }
    }

    async fn call(&self, target: &str, body: serde_json::Value) -> Result<serde_json::Value, (String, String)> {
        let r = self
            .http
            .post(&self.endpoint)
            .header("Content-Type", "application/x-amz-json-1.1")
            .header("X-Amz-Target", format!("AWSCognitoIdentityProviderService.{target}"))
            .json(&body)
            .send()
            .await
            .map_err(|e| ("Network".to_string(), e.to_string()))?;
        let ok = r.status().is_success();
        let v: serde_json::Value = r.json().await.unwrap_or(serde_json::Value::Null);
        if ok {
            return Ok(v);
        }
        let kind = v["__type"].as_str().unwrap_or("Error").rsplit('#').next().unwrap_or("Error").to_string();
        Err((kind, v["message"].as_str().unwrap_or("").to_string()))
    }

    fn refused((kind, msg): (String, String)) -> SignIn {
        SignIn::Refused(match kind.as_str() {
            "NotAuthorizedException" | "UserNotFoundException" => "That name and password do not match.".into(),
            "InvalidPasswordException" => format!("That password is not allowed: {msg}"),
            "PasswordResetRequiredException" => "Your password has to be reset by an admin.".into(),
            "TooManyRequestsException" | "LimitExceededException" => "Too many tries. Wait a minute and try again.".into(),
            "Network" => "Sign-in could not be reached. Try again.".into(),
            _ => format!("Sign-in failed ({kind})."),
        })
    }

    fn done(username: &str, v: &serde_json::Value) -> SignIn {
        // the name the admin gave the account, from the ID token's claims
        let name = v["AuthenticationResult"]["IdToken"]
            .as_str()
            .and_then(|t| t.split('.').nth(1))
            .and_then(|p| B64.decode(p.trim_end_matches('=')).ok())
            .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok())
            .and_then(|c| c["name"].as_str().map(String::from));
        let (u, agency) = Account::parse_username(username).unwrap_or_default();
        SignIn::Done(Account { username: u, agency, name })
    }

    async fn sign_in(&self, username: &str, password: &str) -> SignIn {
        let body = serde_json::json!({
            "AuthFlow": "USER_PASSWORD_AUTH",
            "ClientId": self.client_id,
            "AuthParameters": { "USERNAME": username, "PASSWORD": password },
        });
        match self.call("InitiateAuth", body).await {
            Ok(v) if v["ChallengeName"] == "NEW_PASSWORD_REQUIRED" => {
                SignIn::NewPassword { session: v["Session"].as_str().unwrap_or("").to_string() }
            }
            Ok(v) if v.get("AuthenticationResult").is_some() => Self::done(username, &v),
            Ok(v) => SignIn::Refused(format!(
                "Sign-in asked for a step this app does not support ({}).",
                v["ChallengeName"].as_str().unwrap_or("unknown")
            )),
            Err(e) => Self::refused(e),
        }
    }

    async fn new_password(&self, username: &str, session: &str, new: &str) -> SignIn {
        let body = serde_json::json!({
            "ChallengeName": "NEW_PASSWORD_REQUIRED",
            "ClientId": self.client_id,
            "Session": session,
            "ChallengeResponses": { "USERNAME": username, "NEW_PASSWORD": new },
        });
        match self.call("RespondToAuthChallenge", body).await {
            Ok(v) if v.get("AuthenticationResult").is_some() => Self::done(username, &v),
            Ok(_) => SignIn::Refused("Sign in again with your temporary password.".into()),
            Err(e) => Self::refused(e),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_user_at_agency_is_a_sign_in_name() {
        assert_eq!(Account::parse_username(" Engineer@Napkin "), Some(("engineer@napkin".into(), "napkin".into())));
        assert_eq!(Account::parse_username("john@javelin"), Some(("john@javelin".into(), "javelin".into())));
        for bad in ["engineer", "@napkin", "a@b.com", "a@", "a b@napkin", "a@na pkin", "a@../x", "a@-x"] {
            assert!(Account::parse_username(bad).is_none(), "{bad} must be refused");
        }
    }

    #[test]
    fn a_session_cookie_is_signed_and_runs_out() {
        let s = Sessions::new(&[7u8; 32]).unwrap();
        let a = Account { username: "engineer@napkin".into(), agency: "napkin".into(), name: Some("Shrey".into()) };
        let c = s.issue(&a);
        assert_eq!(s.read(&c), Some(a.clone()));
        // a cookie edited to name someone else fails its signature
        let (body, sig) = c.split_once('.').unwrap();
        let mut forged: serde_json::Value = serde_json::from_slice(&B64.decode(body).unwrap()).unwrap();
        forged["username"] = "visionary@napkin".into();
        let forged = format!("{}.{sig}", B64.encode(serde_json::to_vec(&forged).unwrap()));
        assert_eq!(s.read(&forged), None);
        // another server's key does not read it
        assert_eq!(Sessions::new(&[8u8; 32]).unwrap().read(&c), None);
        assert!(Sessions::new(b"short").is_err());
    }

    #[test]
    fn a_local_account_changes_its_temporary_password_on_first_sign_in() {
        let dir = tempfile::tempdir().unwrap();
        let l = Local::new(dir.path().join("users.json"));
        l.add("engineer@napkin", "Temporary-1234", Some("Shrey")).unwrap();
        let session = match l.sign_in("engineer@napkin", "Temporary-1234") {
            SignIn::NewPassword { session } => session,
            other => panic!("expected the new-password step, got {other:?}"),
        };
        assert!(matches!(l.sign_in("engineer@napkin", "wrong"), SignIn::Refused(_)));
        match l.new_password("engineer@napkin", &session, "MyOwnPassword9") {
            SignIn::Done(a) => assert_eq!((a.username.as_str(), a.agency.as_str()), ("engineer@napkin", "napkin")),
            other => panic!("{other:?}"),
        }
        // the temporary password no longer works; the chosen one does, straight in
        assert!(matches!(l.sign_in("engineer@napkin", "Temporary-1234"), SignIn::Refused(_)));
        assert!(matches!(l.sign_in("engineer@napkin", "MyOwnPassword9"), SignIn::Done(_)));
        // the challenge cannot be answered twice
        assert!(matches!(l.new_password("engineer@napkin", &session, "Another9password"), SignIn::Refused(_)));
    }

    #[test]
    fn a_weak_password_is_refused_with_the_rule() {
        assert!(weak("short1").is_some());
        assert!(weak("onlyletterslong").is_some());
        assert!(weak("letters-and-1-number").is_none());
    }
}
