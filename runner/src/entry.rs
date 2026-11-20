//! Bot directories: a bot packaged with a `bot.toml` that says how to build
//! and run it. This is the form in which benchmark bots are submitted, and a
//! bot directory works wherever a bot command does.

use crate::bot::split_command;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

pub const MANIFEST: &str = "bot.toml";
/// The most a bot directory may hold before it's built.
pub const MAX_BYTES: u64 = 20 * 1024 * 1024;

#[derive(Debug)]
pub struct Entry {
    /// The bot directory, as an absolute path.
    pub dir: PathBuf,
    /// The command that starts the bot.
    pub run: String,
    /// The command that builds it, if it needs building.
    pub build: Option<String>,
}

impl Entry {
    /// Reads the `bot.toml` in `dir`.
    pub fn load(dir: &Path) -> Result<Entry, String> {
        let path = dir.join(MANIFEST);
        let text = std::fs::read_to_string(&path).map_err(|e| format!("{}: {e}", path.display()))?;
        let (mut run, mut build) = (None, None);
        for (n, raw) in text.lines().enumerate() {
            let at = |e: String| format!("{}:{}: {e}", path.display(), n + 1);
            let Some((key, value)) = parse_line(raw).map_err(at)? else {
                continue;
            };
            let slot = match key.as_str() {
                "run" => &mut run,
                "build" => &mut build,
                _ => return Err(at(format!("unknown key '{key}': the keys are run and build"))),
            };
            if split_command(&value).map_err(at)?.is_empty() {
                return Err(at(format!("'{key}' is empty")));
            }
            if slot.replace(value).is_some() {
                return Err(at(format!("'{key}' is set twice")));
            }
        }
        let cwd = std::env::current_dir().map_err(|e| format!("no current directory: {e}"))?;
        Ok(Entry {
            dir: cwd.join(dir),
            run: run.ok_or_else(|| format!("{}: there is no 'run' command", path.display()))?,
            build,
        })
    }

    /// The program and arguments of one of the entry's commands. Those that
    /// name something in the bot directory become absolute paths, since the
    /// bot runs in another directory.
    pub fn args(&self, command: &str) -> Result<Vec<String>, String> {
        let mut args = split_command(command)?;
        for (i, arg) in args.iter_mut().enumerate() {
            let path = self.dir.join(&*arg);
            let exe = format!("{}{}", path.display(), std::env::consts::EXE_SUFFIX);
            let program = i == 0 && Path::new(&exe).is_file();
            if Path::new(&*arg).is_relative() && (path.exists() || program) {
                *arg = path.to_string_lossy().into_owned();
            }
        }
        Ok(args)
    }

    /// The bytes of every file in the bot directory.
    pub fn size(&self) -> Result<u64, String> {
        fn walk(dir: &Path) -> std::io::Result<u64> {
            let mut total = 0;
            for entry in std::fs::read_dir(dir)? {
                let entry = entry?;
                let meta = entry.metadata()?;
                total += if meta.is_dir() {
                    walk(&entry.path())?
                } else {
                    meta.len()
                };
            }
            Ok(total)
        }
        walk(&self.dir).map_err(|e| format!("{}: {e}", self.dir.display()))
    }

    /// Runs the build command in the bot directory, if there is one. The
    /// command's output goes to this process's own.
    pub fn build(&self, timeout: Duration) -> Result<(), String> {
        let Some(command) = &self.build else {
            return Ok(());
        };
        let args = self.args(command)?;
        let mut child = Command::new(&args[0])
            .args(&args[1..])
            .current_dir(&self.dir)
            .stdin(Stdio::null())
            .spawn()
            .map_err(|e| format!("can't start the build '{command}': {e}"))?;
        let deadline = Instant::now() + timeout;
        loop {
            match child.try_wait() {
                Ok(Some(status)) if status.success() => return Ok(()),
                Ok(Some(status)) => return Err(format!("the build '{command}' failed: {status}")),
                Ok(None) if Instant::now() >= deadline => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(format!("the build '{command}' took over {} seconds", timeout.as_secs()));
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(50)),
                Err(e) => return Err(format!("the build '{command}': {e}")),
            }
        }
    }
}

/// If `command` names a bot directory, the program and arguments that start
/// its bot.
pub fn resolve(command: &str) -> Result<Option<Vec<String>>, String> {
    let dir = Path::new(command.trim());
    if !dir.join(MANIFEST).is_file() {
        return Ok(None);
    }
    let entry = Entry::load(dir)?;
    entry.args(&entry.run).map(Some)
}

/// Parses a `key = "value"` line. Blank lines and comments give `None`.
fn parse_line(raw: &str) -> Result<Option<(String, String)>, String> {
    let line = raw.trim();
    if line.is_empty() || line.starts_with('#') {
        return Ok(None);
    }
    let expected = || "expected: key = \"value\"".to_string();
    let (key, rest) = line.split_once('=').ok_or_else(expected)?;
    let mut chars = rest.trim().chars();
    let quote = chars.next().filter(|c| *c == '"' || *c == '\'').ok_or_else(expected)?;
    let mut value = String::new();
    loop {
        match chars.next().ok_or("unclosed quote")? {
            c if c == quote => break,
            '\\' if quote == '"' => match chars.next() {
                Some(c @ ('"' | '\\')) => value.push(c),
                _ => return Err("the only escapes are \\\" and \\\\; write paths with / or in single quotes".into()),
            },
            c => value.push(c),
        }
    }
    let tail = chars.as_str().trim();
    if !tail.is_empty() && !tail.starts_with('#') {
        return Err(format!("unexpected '{tail}' after the value"));
    }
    Ok(Some((key.trim().to_string(), value)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manifest_lines() {
        let ok = |line: &str| parse_line(line).unwrap();
        assert_eq!(ok("  # a comment"), None);
        assert_eq!(
            ok("run = \"python bot.py\"  # how"),
            Some(("run".into(), "python bot.py".into()))
        );
        assert_eq!(ok("run='a \"b c\" #d'"), Some(("run".into(), "a \"b c\" #d".into())));
        assert_eq!(ok(r#"run = "a \"b\" \\""#), Some(("run".into(), r#"a "b" \"#.into())));
        for bad in ["run", "run = python", "run = \"x", "run = \"x\" y", r#"run = "a\nb""#] {
            assert!(parse_line(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn entries() {
        let dir = std::env::temp_dir().join(format!("bitbath-entry-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("src")).unwrap();
        let write = |text: &str| std::fs::write(dir.join(MANIFEST), text).unwrap();
        std::fs::write(dir.join("bot.py"), "12345").unwrap();
        std::fs::write(dir.join("src").join("lib.py"), "123").unwrap();

        let manifest = "# my bot\nrun = \"python bot.py --fast\"\n";
        write(manifest);
        let entry = Entry::load(&dir).unwrap();
        assert!(entry.build.is_none());
        let args = entry.args(&entry.run).unwrap();
        assert_eq!((args[0].as_str(), args[2].as_str()), ("python", "--fast"));
        assert_eq!(Path::new(&args[1]), dir.join("bot.py"));
        assert_eq!(entry.size().unwrap(), 5 + 3 + manifest.len() as u64);
        assert_eq!(resolve(&dir.to_string_lossy()).unwrap(), Some(args));
        entry.build(Duration::from_secs(1)).unwrap();

        for bad in [
            "build = \"make\"\n",
            "run = \"a\"\nrun = \"b\"\n",
            "run = \"\"\n",
            "ruin = \"a\"\n",
        ] {
            write(bad);
            assert!(Entry::load(&dir).is_err(), "{bad}");
        }
        write("run = \"python bot.py\"\nbuild = \"no-such-program-here\"\n");
        let err = Entry::load(&dir).unwrap().build(Duration::from_secs(1)).unwrap_err();
        assert!(err.contains("can't start the build"), "{err}");

        std::fs::remove_dir_all(&dir).unwrap();
        assert_eq!(resolve(&dir.to_string_lossy()).unwrap(), None);
        assert_eq!(resolve("python bots/greedy_bot.py").unwrap(), None);
    }
}
