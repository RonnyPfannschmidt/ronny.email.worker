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
