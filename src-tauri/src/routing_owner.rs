use crate::app_flavor::RoutingOwner;
use std::{error::Error, fmt};

/// Codex overlay mutation: restore Official config, or write the Hub session overlay.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OverlayMode {
    Official,
    Hub,
}

/// Kind of routing-owner mutation. Kept distinct so History conservatism and the
/// managed-client Official fork cannot silently reuse another gate's rules.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MutationKind {
    CodexOverlay { mode: OverlayMode },
    ManagedClient,
    HistoryRepair,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OwnerError {
    TakeoverRequired {
        current: RoutingOwner,
        target: Option<RoutingOwner>,
    },
    OwnerMismatch {
        current: RoutingOwner,
        target: Option<RoutingOwner>,
    },
}

impl OwnerError {
    pub fn code(self) -> &'static str {
        match self {
            Self::TakeoverRequired { .. } => "route.takeover_required",
            Self::OwnerMismatch { .. } => "route.owner_mismatch",
        }
    }
}

impl fmt::Display for OwnerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let (current, target) = match *self {
            Self::TakeoverRequired { current, target }
            | Self::OwnerMismatch { current, target } => (current, target),
        };
        write!(
            formatter,
            "{}: Codex target owner is {:?}; current channel owner is {:?}",
            self.code(),
            target,
            current
        )
    }
}

impl Error for OwnerError {}

fn never_mutates(owner: RoutingOwner) -> bool {
    matches!(
        owner,
        RoutingOwner::Official | RoutingOwner::UnknownExternal
    )
}

fn release_may_mutate_without_takeover(target: Option<RoutingOwner>) -> bool {
    matches!(target, None | Some(RoutingOwner::Official))
}

/// Decide whether the current owner may mutate a target.
///
/// Rules:
/// - Official/Unknown never mutate.
/// - Beta only mutates Beta unless force_takeover.
/// - Release may mutate Official/none without takeover; mutating Beta requires takeover.
/// - HistoryRepair ignores force_takeover (blocked stays blocked).
/// - ManagedClient always allows an Official target (existing managed-client fork).
pub fn permit(
    current: RoutingOwner,
    target: Option<RoutingOwner>,
    kind: MutationKind,
    force_takeover: bool,
) -> Result<(), OwnerError> {
    if never_mutates(current) {
        return Err(OwnerError::OwnerMismatch { current, target });
    }

    match kind {
        // HistoryRepair has no takeover knob: force_takeover is ignored.
        MutationKind::HistoryRepair => permit_history(current, target),
        MutationKind::ManagedClient => permit_managed(current, target, force_takeover),
        MutationKind::CodexOverlay { mode } => {
            permit_overlay(current, target, mode, force_takeover)
        }
    }
}

fn permit_history(current: RoutingOwner, target: Option<RoutingOwner>) -> Result<(), OwnerError> {
    let allowed = match current {
        RoutingOwner::Release => target != Some(RoutingOwner::Beta),
        RoutingOwner::Beta => target == Some(RoutingOwner::Beta),
        RoutingOwner::Official | RoutingOwner::UnknownExternal => false,
    };
    if allowed {
        Ok(())
    } else {
        Err(OwnerError::TakeoverRequired { current, target })
    }
}

fn permit_managed(
    current: RoutingOwner,
    target: Option<RoutingOwner>,
    force_takeover: bool,
) -> Result<(), OwnerError> {
    if target == Some(RoutingOwner::Official) || target == Some(current) {
        return Ok(());
    }
    if force_takeover {
        return Ok(());
    }
    Err(OwnerError::TakeoverRequired { current, target })
}

fn permit_overlay(
    current: RoutingOwner,
    target: Option<RoutingOwner>,
    mode: OverlayMode,
    force_takeover: bool,
) -> Result<(), OwnerError> {
    if target == Some(current) {
        return Ok(());
    }
    if force_takeover {
        return Ok(());
    }
    if current == RoutingOwner::Release && release_may_mutate_without_takeover(target) {
        return Ok(());
    }
    match mode {
        OverlayMode::Hub => Err(OwnerError::TakeoverRequired { current, target }),
        OverlayMode::Official => Err(OwnerError::OwnerMismatch { current, target }),
    }
}

#[cfg(test)]
mod tests {
    use super::{permit, MutationKind, OverlayMode, OwnerError};
    use crate::app_flavor::RoutingOwner;

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    enum Expect {
        Allow,
        TakeoverRequired,
        OwnerMismatch,
    }

    fn classify(result: Result<(), OwnerError>) -> Expect {
        match result {
            Ok(()) => Expect::Allow,
            Err(OwnerError::TakeoverRequired { .. }) => Expect::TakeoverRequired,
            Err(OwnerError::OwnerMismatch { .. }) => Expect::OwnerMismatch,
        }
    }

    /// Spec-shaped expected outcome. Structured by the documented rules rather
    /// than by copying permit's kind dispatch, so a flattened History or
    /// dropped ManagedClient Official fork fails this matrix.
    fn expected(
        current: RoutingOwner,
        target: Option<RoutingOwner>,
        kind: MutationKind,
        force_takeover: bool,
    ) -> Expect {
        if matches!(
            current,
            RoutingOwner::Official | RoutingOwner::UnknownExternal
        ) {
            return Expect::OwnerMismatch;
        }

        if kind == MutationKind::HistoryRepair {
            return match (current, target) {
                (RoutingOwner::Release, Some(RoutingOwner::Beta)) => Expect::TakeoverRequired,
                (RoutingOwner::Beta, Some(RoutingOwner::Beta)) => Expect::Allow,
                (RoutingOwner::Beta, _) => Expect::TakeoverRequired,
                (RoutingOwner::Release, _) => Expect::Allow,
                _ => Expect::OwnerMismatch,
            };
        }

        if target == Some(current) {
            return Expect::Allow;
        }
        if force_takeover {
            return Expect::Allow;
        }

        match kind {
            MutationKind::ManagedClient => {
                if target == Some(RoutingOwner::Official) {
                    Expect::Allow
                } else {
                    Expect::TakeoverRequired
                }
            }
            MutationKind::CodexOverlay { mode } => {
                let release_compat = current == RoutingOwner::Release
                    && matches!(target, None | Some(RoutingOwner::Official));
                if release_compat {
                    Expect::Allow
                } else if mode == OverlayMode::Hub {
                    Expect::TakeoverRequired
                } else {
                    Expect::OwnerMismatch
                }
            }
            MutationKind::HistoryRepair => unreachable!("history handled above"),
        }
    }

    #[test]
    fn routing_owner_permit_matrix() {
        let currents = [
            RoutingOwner::Official,
            RoutingOwner::Release,
            RoutingOwner::Beta,
            RoutingOwner::UnknownExternal,
        ];
        let targets = [
            None,
            Some(RoutingOwner::Official),
            Some(RoutingOwner::Release),
            Some(RoutingOwner::Beta),
            Some(RoutingOwner::UnknownExternal),
        ];
        let kinds = [
            MutationKind::CodexOverlay {
                mode: OverlayMode::Official,
            },
            MutationKind::CodexOverlay {
                mode: OverlayMode::Hub,
            },
            MutationKind::ManagedClient,
            MutationKind::HistoryRepair,
        ];

        for current in currents {
            for target in targets {
                for kind in kinds {
                    for force_takeover in [false, true] {
                        let got = classify(permit(current, target, kind, force_takeover));
                        let want = expected(current, target, kind, force_takeover);
                        assert_eq!(
                            got,
                            want,
                            "current={current:?} target={target:?} kind={kind:?} force={force_takeover}"
                        );
                    }
                }
            }
        }
    }
}
