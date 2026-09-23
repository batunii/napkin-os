// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Who is asking, resolved before the OS layer sees the request.
//!
//! A shell authenticates; the layer never does. Whatever a request body says
//! about who wrote it is a *claim*, recorded as one — attribution, scope and
//! the branch namespace come from the [`Ctx`] the shell built (contract §3,
//! M3). That is what lets the same operations run under a desktop with one
//! local user, a browser tab, and a server holding many tenants.

use std::fmt;

use crate::error::{HostError, HostResult};

/// The principal an operation runs as. One of three shapes, so a reader of
/// the decision chain can tell a person from a job from a piece of software:
///
/// - `human:<user-id>` — a person, as the shell authenticated them.
/// - `process:<job-id>` — a background job acting on someone's behalf.
/// - `<producer>/<version>` — a tool writing under its own name.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Actor(String);

impl Actor {
    pub fn human(id: &str) -> HostResult<Self> {
        Self::parse(&format!("human:{id}"))
    }

    pub fn process(job: &str) -> HostResult<Self> {
        Self::parse(&format!("process:{job}"))
    }

    pub fn producer(name: &str, version: &str) -> HostResult<Self> {
        Self::parse(&format!("{name}/{version}"))
    }

    /// Accept only the three shapes. An actor ends up in every decision the
    /// layer records, so a malformed one is refused here rather than stored.
    pub fn parse(s: &str) -> HostResult<Self> {
        let well_formed = |part: &str| {
            !part.is_empty() && !part.chars().any(|c| c.is_whitespace() || c.is_control())
        };
        let ok = if let Some(id) = s.strip_prefix("human:") {
            well_formed(id)
        } else if let Some(job) = s.strip_prefix("process:") {
            well_formed(job)
        } else if let Some((name, version)) = s.split_once('/') {
            well_formed(name) && well_formed(version) && !name.contains(':')
        } else {
            false
        };
        if ok {
            Ok(Self(s.to_string()))
        } else {
            Err(HostError::bad_request(format!(
                "malformed actor {s:?}: expected human:<id>, process:<id> or <producer>/<version>"
            )))
        }
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    pub fn is_human(&self) -> bool {
        self.0.starts_with("human:")
    }
}

impl fmt::Display for Actor {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// The org and brand an operation is scoped to, as the shell resolved them
/// from the authenticated session — never from the body.
///
/// Opaque to the layer: it records them and compares them, it never parses
/// them. Empty on the desktop and in the browser build, where there is one
/// user and no tenancy.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Scope {
    pub org: Option<String>,
    pub brand: Option<String>,
}

impl Scope {
    pub fn is_empty(&self) -> bool {
        self.org.is_none() && self.brand.is_none()
    }
}

/// Everything an operation needs to know about who is asking.
///
/// Built by the shell once per request (the server) or once per process (the
/// desktop), and threaded through every operation that records anything.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Ctx {
    pub actor: Actor,
    pub scope: Scope,
    /// `name@major.minor`, set when a handler is acting rather than a person.
    pub handler: Option<String>,
    /// Set when a judgement port answered, so two documents from the same app
    /// that differ can say why.
    pub backend: Option<String>,
}

impl Ctx {
    pub fn new(actor: Actor) -> Self {
        Self {
            actor,
            scope: Scope::default(),
            handler: None,
            backend: None,
        }
    }

    /// The desktop and browser shells: one person, on their own machine, with
    /// no tenancy. There is nobody else it could be.
    pub fn local() -> Self {
        Self::new(Actor(LOCAL_HUMAN.to_string()))
    }

    pub fn with_scope(mut self, scope: Scope) -> Self {
        self.scope = scope;
        self
    }

    pub fn with_handler(mut self, handler: impl Into<String>) -> Self {
        self.handler = Some(handler.into());
        self
    }

    pub fn with_backend(mut self, backend: impl Into<String>) -> Self {
        self.backend = Some(backend.into());
        self
    }
}

/// The one local user of a desktop or browser shell.
pub const LOCAL_HUMAN: &str = "human:local";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn actors_take_exactly_the_three_shapes() {
        assert!(Actor::parse("human:u-123").is_ok());
        assert!(Actor::parse("process:job-9").is_ok());
        assert!(Actor::parse("napkin-studio/1.2.1").is_ok());
        for bad in [
            "",
            "human",
            "human:",
            "process:",
            "claude",
            "/1.0",
            "tool/",
            "human:has space",
            "a:b/c",
        ] {
            assert!(Actor::parse(bad).is_err(), "{bad:?} must be refused");
        }
    }

    #[test]
    fn the_local_context_is_a_human_with_no_scope() {
        let ctx = Ctx::local();
        assert!(ctx.actor.is_human());
        assert!(ctx.scope.is_empty());
        assert_eq!(ctx.actor.as_str(), LOCAL_HUMAN);
    }
}
