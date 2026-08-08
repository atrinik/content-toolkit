// Copyright 2026 The Atrinik Project
// SPDX-License-Identifier: MIT

#![forbid(unsafe_code)]

use std::fmt;

use sha2::{Digest, Sha256};

const MAGIC: &[u8; 4] = b"ATF1";
const HEADER_BYTES: usize = 40;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Artifact {
    payload: Vec<u8>,
    digest: [u8; 32],
}

impl Artifact {
    pub fn compile(payload: &[u8], maximum_payload: usize) -> Result<Vec<u8>, Error> {
        if payload.len() > maximum_payload || payload.len() > u32::MAX as usize {
            return Err(Error::LimitExceeded);
        }
        let digest: [u8; 32] = Sha256::digest(payload).into();
        let mut encoded = Vec::with_capacity(HEADER_BYTES + payload.len());
        encoded.extend_from_slice(MAGIC);
        encoded.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        encoded.extend_from_slice(&digest);
        encoded.extend_from_slice(payload);
        Ok(encoded)
    }

    pub fn decode(encoded: &[u8], maximum_payload: usize) -> Result<Self, Error> {
        let header = encoded.get(..HEADER_BYTES).ok_or(Error::Truncated)?;
        if &header[..4] != MAGIC {
            return Err(Error::UnsupportedVersion);
        }
        let length =
            u32::from_le_bytes(header[4..8].try_into().map_err(|_| Error::Truncated)?) as usize;
        if length > maximum_payload {
            return Err(Error::LimitExceeded);
        }
        let expected = HEADER_BYTES
            .checked_add(length)
            .ok_or(Error::LimitExceeded)?;
        if encoded.len() != expected {
            return Err(Error::Truncated);
        }
        let payload = encoded[HEADER_BYTES..].to_vec();
        let digest: [u8; 32] = Sha256::digest(&payload).into();
        if digest.as_slice() != &header[8..40] {
            return Err(Error::Integrity);
        }
        Ok(Self { payload, digest })
    }

    #[must_use]
    pub fn payload(&self) -> &[u8] {
        &self.payload
    }

    #[must_use]
    pub const fn digest(&self) -> [u8; 32] {
        self.digest
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Error {
    Truncated,
    UnsupportedVersion,
    LimitExceeded,
    Integrity,
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "invalid foundation artifact: {self:?}")
    }
}

impl std::error::Error for Error {}

#[cfg(test)]
mod tests {
    use super::{Artifact, Error};

    #[test]
    fn is_deterministic_bounded_and_integrity_checked() {
        let encoded = Artifact::compile(b"canonical", 32).unwrap();
        assert_eq!(encoded, Artifact::compile(b"canonical", 32).unwrap());
        assert_eq!(
            Artifact::decode(&encoded, 32).unwrap().payload(),
            b"canonical"
        );
        let mut corrupt = encoded;
        *corrupt.last_mut().unwrap() ^= 1;
        assert_eq!(Artifact::decode(&corrupt, 32), Err(Error::Integrity));
    }
}
