//! Bot processes (SPEC §10): spawning, line I/O on their own threads, and
//! killing the whole process tree afterwards.

use std::io::{BufRead, BufReader, Read, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

pub enum Event {
    /// A line from the bot's stdout, without its line ending.
    Line(String),
    /// A line over the length limit. Holds its first few characters.
    TooLong(String),
    /// stdout closed, which means the bot exited or can't talk any more.
    Eof,
}

/// stderr captured since it was last taken, up to a cap.
struct StderrBuf {
    text: Vec<u8>,
    dropped: usize,
    cap: usize,
}

pub struct Bot {
    child: Child,
    events: Receiver<Event>,
    input: Option<Sender<String>>,
    stderr: Arc<Mutex<StderrBuf>>,
    job: Option<job::Job>,
    eof: bool,
}

impl Bot {
    /// Starts `command` in `dir`. Arguments that name existing files relative
    /// to the current directory are made absolute first, so a command like
    /// `python bots/greedy.py` still works from the bot's own directory.
    /// A command that names a bot directory starts the bot its `bot.toml`
    /// describes.
    pub fn spawn(command: &str, dir: &Path, memory_mb: u64, max_line: usize, stderr_cap: usize) -> Result<Bot, String> {
        let entry = crate::entry::resolve(command)?;
        let args: Vec<String> = match &entry {
            Some((_, args)) => args.clone(),
            None => split_command(command)?.iter().map(|a| absolutize(a)).collect(),
        };
        let (program, rest) = args.split_first().ok_or("the bot command is empty")?;
        let mut cmd = Command::new(program);
        cmd.args(rest)
            .current_dir(dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .env("PYTHONUNBUFFERED", "1");
        if let Some((entry, _)) = &entry {
            entry.run_as_owner(&mut cmd, dir)?;
        }
        job::prepare(&mut cmd, memory_mb);
        let mut child = cmd.spawn().map_err(|e| format!("can't start '{command}': {e}"))?;
        let job = job::attach(&child, memory_mb);

        let (event_tx, events) = mpsc::channel();
        let stdout = child.stdout.take().expect("stdout is piped");
        thread::spawn(move || read_lines(stdout, event_tx, max_line));

        let (input, input_rx) = mpsc::channel::<String>();
        let stdin = child.stdin.take().expect("stdin is piped");
        thread::spawn(move || write_lines(stdin, input_rx));

        let stderr = Arc::new(Mutex::new(StderrBuf {
            text: Vec::new(),
            dropped: 0,
            cap: stderr_cap,
        }));
        let err_pipe = child.stderr.take().expect("stderr is piped");
        let sink = Arc::clone(&stderr);
        thread::spawn(move || read_stderr(err_pipe, sink));

        Ok(Bot {
            child,
            events,
            input: Some(input),
            stderr,
            job,
            eof: false,
        })
    }

    /// Queues lines for the bot. The writer thread never blocks the caller.
    pub fn send<S: AsRef<str>>(&self, lines: &[S]) {
        if let Some(input) = &self.input {
            let mut text = String::new();
            for line in lines {
                text.push_str(line.as_ref());
                text.push('\n');
            }
            let _ = input.send(text);
        }
    }

    /// Closes the bot's stdin once queued lines are written.
    pub fn close_input(&mut self) {
        self.input = None;
    }

    /// The next event, waiting until `deadline`. `None` means the deadline passed.
    pub fn recv(&mut self, deadline: Instant) -> Option<Event> {
        if self.eof {
            return Some(Event::Eof);
        }
        match self
            .events
            .recv_timeout(deadline.saturating_duration_since(Instant::now()))
        {
            Ok(Event::Eof) | Err(RecvTimeoutError::Disconnected) => {
                self.eof = true;
                Some(Event::Eof)
            }
            Ok(event) => Some(event),
            Err(RecvTimeoutError::Timeout) => None,
        }
    }

    /// The next event if one is already waiting.
    pub fn try_recv(&mut self) -> Option<Event> {
        self.recv(Instant::now())
    }

    /// Takes the stderr captured so far.
    pub fn take_stderr(&self) -> String {
        let mut buf = self.stderr.lock().expect("stderr lock");
        let mut text = String::from_utf8_lossy(&buf.text).into_owned();
        if buf.dropped > 0 {
            text.push_str(&format!("\n[{} more bytes of stderr dropped]", buf.dropped));
        }
        buf.text.clear();
        buf.dropped = 0;
        text
    }

    /// Why the bot stopped talking: `memory` if it ran into its memory cap,
    /// otherwise `crash`. Also returns a description with the exit status.
    pub fn exit_reason(&mut self) -> (&'static str, String) {
        let deadline = Instant::now() + Duration::from_millis(500);
        let status = loop {
            match self.child.try_wait() {
                Ok(Some(status)) => break Some(status),
                Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
                _ => break None,
            }
        };
        let what = match status {
            Some(s) => format!("exited with {s}"),
            None => "closed its output".to_string(),
        };
        if self.job.as_ref().is_some_and(|j| j.hit_memory_limit()) {
            ("memory", format!("{what} after reaching its memory cap"))
        } else {
            ("crash", what)
        }
    }

    /// Waits up to `grace` for the bot to exit by itself, then kills its
    /// whole process tree.
    pub fn shut_down(&mut self, grace: Duration) {
        self.close_input();
        let deadline = Instant::now() + grace;
        while Instant::now() < deadline {
            if let Ok(Some(_)) = self.child.try_wait() {
                break;
            }
            thread::sleep(Duration::from_millis(10));
        }
        self.kill();
    }

    fn kill(&mut self) {
        if let Some(job) = &self.job {
            job.kill();
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

impl Drop for Bot {
    fn drop(&mut self) {
        self.kill();
    }
}

/// Splits a command line on whitespace, honoring single and double quotes.
pub fn split_command(s: &str) -> Result<Vec<String>, String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut quote: Option<char> = None;
    let mut quoted = false;
    for c in s.chars() {
        match quote {
            Some(q) if c == q => quote = None,
            Some(_) => cur.push(c),
            None if c == '"' || c == '\'' => {
                quote = Some(c);
                quoted = true;
            }
            None if c.is_whitespace() => {
                if quoted || !cur.is_empty() {
                    out.push(std::mem::take(&mut cur));
                    quoted = false;
                }
            }
            None => cur.push(c),
        }
    }
    if quote.is_some() {
        return Err(format!("unclosed quote in command: {s}"));
    }
    if quoted || !cur.is_empty() {
        out.push(cur);
    }
    Ok(out)
}

fn absolutize(arg: &str) -> String {
    let path = Path::new(arg);
    if path.is_relative() && path.exists() {
        if let Ok(cwd) = std::env::current_dir() {
            return cwd.join(path).to_string_lossy().into_owned();
        }
    }
    arg.to_string()
}

fn read_lines(out: impl Read, tx: Sender<Event>, max: usize) {
    let mut reader = BufReader::new(out);
    let mut line: Vec<u8> = Vec::new();
    let mut too_long = false;
    let finish = |line: &mut Vec<u8>, too_long: bool| {
        if line.last() == Some(&b'\r') {
            line.pop();
        }
        let text = String::from_utf8_lossy(line).into_owned();
        line.clear();
        if too_long {
            Event::TooLong(text)
        } else {
            Event::Line(text)
        }
    };
    loop {
        let (consumed, newline) = match reader.fill_buf() {
            Ok(buf) if !buf.is_empty() => {
                let newline = buf.iter().position(|&b| b == b'\n');
                let chunk = &buf[..newline.unwrap_or(buf.len())];
                if !too_long {
                    line.extend_from_slice(chunk);
                    if line.len() > max {
                        too_long = true;
                        line.truncate(32);
                    }
                }
                (newline.map_or(buf.len(), |i| i + 1), newline.is_some())
            }
            _ => break,
        };
        reader.consume(consumed);
        if newline {
            if tx.send(finish(&mut line, too_long)).is_err() {
                return;
            }
            too_long = false;
        }
    }
    if !line.is_empty() {
        let _ = tx.send(finish(&mut line, too_long));
    }
    let _ = tx.send(Event::Eof);
}

fn write_lines(mut stdin: ChildStdin, rx: Receiver<String>) {
    for text in rx {
        if stdin.write_all(text.as_bytes()).and_then(|_| stdin.flush()).is_err() {
            break;
        }
    }
}

fn read_stderr(mut err: impl Read, sink: Arc<Mutex<StderrBuf>>) {
    let mut chunk = [0u8; 4096];
    loop {
        match err.read(&mut chunk) {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                let mut buf = sink.lock().expect("stderr lock");
                let keep = n.min(buf.cap.saturating_sub(buf.text.len()));
                buf.text.extend_from_slice(&chunk[..keep]);
                buf.dropped += n - keep;
            }
        }
    }
}

#[cfg(windows)]
mod job {
    //! A Job Object per bot: a memory cap for the whole process tree, and
    //! killing the tree when the job closes.

    use std::os::windows::io::AsRawHandle;
    use std::process::{Child, Command};
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation, QueryInformationJobObject,
        SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_JOB_MEMORY,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    pub struct Job {
        handle: HANDLE,
        limit: u64,
    }

    // SAFETY: a job handle isn't tied to the thread that opened it, and the
    // struct owns its handle. `bitbath serve` hands bots between threads.
    unsafe impl Send for Job {}

    pub fn prepare(_cmd: &mut Command, _memory_mb: u64) {}

    pub fn attach(child: &Child, memory_mb: u64) -> Option<Job> {
        let limit = memory_mb * 1024 * 1024;
        // SAFETY: plain Win32 calls on a handle this function owns, with a
        // zeroed, correctly sized limit structure.
        unsafe {
            let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if handle.is_null() {
                return None;
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if limit > 0 {
                info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_JOB_MEMORY;
                info.JobMemoryLimit = limit as usize;
            }
            let size = std::mem::size_of_val(&info) as u32;
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const _,
                size,
            );
            AssignProcessToJobObject(handle, child.as_raw_handle() as HANDLE);
            Some(Job { handle, limit })
        }
    }

    impl Job {
        pub fn kill(&self) {
            // SAFETY: the handle stays open until drop.
            unsafe {
                TerminateJobObject(self.handle, 1);
            }
        }

        /// Whether the job's peak memory reached 90% of the cap. An allocation
        /// that fails can leave the peak well short of the cap itself.
        pub fn hit_memory_limit(&self) -> bool {
            if self.limit == 0 {
                return false;
            }
            // SAFETY: as in `attach`.
            unsafe {
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                let size = std::mem::size_of_val(&info) as u32;
                let ok = QueryInformationJobObject(
                    self.handle,
                    JobObjectExtendedLimitInformation,
                    &mut info as *mut _ as *mut _,
                    size,
                    std::ptr::null_mut(),
                );
                ok != 0 && info.PeakJobMemoryUsed as u64 >= self.limit / 10 * 9
            }
        }
    }

    impl Drop for Job {
        fn drop(&mut self) {
            // SAFETY: closing the handle this struct owns; the job kills
            // anything left in it.
            unsafe {
                CloseHandle(self.handle);
            }
        }
    }
}

#[cfg(unix)]
mod job {
    //! A process group per bot, with an address-space limit as the memory cap.

    use std::os::unix::process::CommandExt;
    use std::process::{Child, Command};

    pub struct Job {
        pgid: i32,
    }

    pub fn prepare(cmd: &mut Command, memory_mb: u64) {
        cmd.process_group(0);
        if memory_mb > 0 {
            let bytes = (memory_mb * 1024 * 1024) as libc::rlim_t;
            // SAFETY: setrlimit is async-signal-safe, so it may run between
            // fork and exec.
            unsafe {
                cmd.pre_exec(move || {
                    let lim = libc::rlimit {
                        rlim_cur: bytes,
                        rlim_max: bytes,
                    };
                    libc::setrlimit(libc::RLIMIT_AS, &lim);
                    Ok(())
                });
            }
        }
    }

    pub fn attach(child: &Child, _memory_mb: u64) -> Option<Job> {
        Some(Job {
            pgid: child.id() as i32,
        })
    }

    impl Job {
        pub fn kill(&self) {
            // SAFETY: signals the bot's own process group.
            unsafe {
                libc::kill(-self.pgid, libc::SIGKILL);
            }
        }

        pub fn hit_memory_limit(&self) -> bool {
            false
        }
    }
}

#[cfg(not(any(windows, unix)))]
mod job {
    use std::process::{Child, Command};

    pub struct Job;

    pub fn prepare(_cmd: &mut Command, _memory_mb: u64) {}

    pub fn attach(_child: &Child, _memory_mb: u64) -> Option<Job> {
        None
    }

    impl Job {
        pub fn kill(&self) {}

        pub fn hit_memory_limit(&self) -> bool {
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::split_command;

    #[test]
    fn splits_commands() {
        assert_eq!(
            split_command("python bots/greedy.py").unwrap(),
            vec!["python", "bots/greedy.py"]
        );
        assert_eq!(
            split_command(r#""C:\My Bots\a.exe" --x 'y z'"#).unwrap(),
            vec![r"C:\My Bots\a.exe", "--x", "y z"]
        );
        assert_eq!(split_command("a \"\" b").unwrap(), vec!["a", "", "b"]);
        assert!(split_command("a 'b").is_err());
    }
}
