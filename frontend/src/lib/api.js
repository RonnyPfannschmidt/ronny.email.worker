async function answer(response) {
  if (response.ok) return response.json();
  const failed = new Error(`the service answered ${response.status}`);
  failed.status = response.status;
  throw failed;
}

/** What a view says when its data is not there, rather than a status code. */
export const missing = (problem, what) => (problem.status === 404 ? `There is no ${what}.` : problem.message);

// The service writes it into the shell; every change carries it.
const csrf = () => document.querySelector('meta[name="mailmind-csrf"]')?.content ?? "";

export const get = (path) => fetch(path).then(answer);

/** `fields` are pairs, so that a name can be there more than once. */
export const post = (path, fields = []) =>
  fetch(path, {
    method: "POST",
    body: new URLSearchParams([["_csrf", csrf()], ...fields]),
  }).then(answer);

/** What each view last read, so coming back to it draws at once and refreshes behind. */
export const cache = new Map();

/** Reads `path` into the cache; the view draws from what comes back. */
export async function read(path) {
  const fresh = await get(path);
  cache.set(path, fresh);
  return fresh;
}

// One stream for the whole session, whichever views come and go: a browser allows a host
// six connections, and a stream per page held one each until the next page queued.
const heard = { ready: new Set(), task: new Set(), queue: new Set() };
let source = null;

function connect() {
  if (source) return;
  source = new EventSource("/api/events");
  source.addEventListener("ready", () => heard.ready.forEach((then) => then()));
  source.addEventListener("task", (event) => {
    const task = JSON.parse(event.data);
    heard.task.forEach((then) => then(task));
  });
  source.addEventListener("queue", (event) => {
    const { bundles } = JSON.parse(event.data);
    heard.queue.forEach((then) => then(bundles));
  });
}

/** Reads now, and again on every reconnection, so whatever was missed is read again.
    Returns what stops listening, for a view to hand back when it goes. */
export function listen(onReady, onTask, onQueue) {
  connect();
  const added = [
    [heard.ready, onReady],
    [heard.task, onTask],
    [heard.queue, onQueue],
  ].filter(([, then]) => then);
  for (const [set, then] of added) set.add(then);
  onReady();
  return () => added.forEach(([set, then]) => set.delete(then));
}

export const taskIsLive = (task) => ["queued", "running"].includes(task?.status);

/** Runs `read` once at a time: asking during a run asks for one more after it, not for a
    second one racing it. */
export function oneAtATime(read) {
  let reading = null;
  let again = false;
  async function run() {
    do {
      again = false;
      await read();
    } while (again);
  }
  return () => {
    if (reading) again = true;
    else reading = run().finally(() => (reading = null));
    return reading;
  };
}
