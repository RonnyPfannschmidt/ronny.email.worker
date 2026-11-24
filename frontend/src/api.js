async function answer(response) {
  if (!response.ok) throw new Error(`the service answered ${response.status}`);
  return response.json();
}

export const get = (path) => fetch(path).then(answer);

export const post = (path, csrf) =>
  fetch(path, { method: "POST", body: new URLSearchParams({ _csrf: csrf }) }).then(answer);

/** Calls `onReady` on every (re)connection, so whatever was missed is read again. */
export function listen(onReady, onTask) {
  const source = new EventSource("/api/events");
  source.addEventListener("ready", onReady);
  source.addEventListener("task", (event) => onTask(JSON.parse(event.data)));
  return () => source.close();
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
