const chatDiv = document.getElementById("chat");
const promptInput = document.getElementById("prompt");
const sendBtn = document.getElementById("send");
const stopBtn = document.getElementById("stop");
const clearBtn = document.getElementById("clear");
const projectSelect = document.getElementById("project");
const addProjectBtn = document.getElementById("addProject");
const deleteProjectBtn = document.getElementById("deleteProject");
const loginBtn = document.getElementById("loginBtn");
const loginContainer = document.getElementById("login-button-container");

let currentAbortController = null;
let googleUserToken = null;

// ---------- Google Identity Services ----------
window.addEventListener("load", () => {
  if (!window.GOOGLE_CLIENT_ID) {
    console.error("GOOGLE_CLIENT_ID is not configured.");
    loginContainer.textContent = "Google sign-in is not configured.";
  } else if (typeof google === "undefined") {
    console.error("Google Identity Services did not load.");
    loginContainer.textContent = "Google sign-in failed to load. Refresh and try again.";
  } else {
    google.accounts.id.initialize({
      client_id: window.GOOGLE_CLIENT_ID,
      callback: handleGoogleCredentialResponse,
    });
    google.accounts.id.renderButton(loginContainer, {
      theme: "outline",
      size: "large",
      text: "signin_with",
    });
  }
  checkLocalAuth();
});

function handleGoogleCredentialResponse(response) {
  googleUserToken = response.credential;
  localStorage.setItem("google_token", googleUserToken);
  updateAuthUI(true);
}

function checkLocalAuth() {
  const savedToken = localStorage.getItem("google_token");
  if (savedToken) {
    googleUserToken = savedToken;
    updateAuthUI(true);
  } else {
    updateAuthUI(false);
  }
}

loginBtn.addEventListener("click", () => {
  googleUserToken = null;
  localStorage.removeItem("google_token");
  if (typeof google !== "undefined") {
    google.accounts.id.disableAutoSelect();
  }
  updateAuthUI(false);
});

function updateAuthUI(isAuthenticated) {
  if (isAuthenticated) {
    loginBtn.textContent = "Log Out";
    loginBtn.hidden = false;
    loginContainer.hidden = true;
    refreshProjects();
  } else {
    loginBtn.hidden = true;
    loginContainer.hidden = false;
    chatDiv.innerHTML = '<div class="message assistant"><em>Please log in to use the coding assistant.</em></div>';
  }
}

async function getHeaders(includeContentType = true) {
  if (!googleUserToken) {
    throw new Error("User not authenticated with Google.");
  }

  const headers = {};
  headers["Authorization"] = `Bearer ${googleUserToken}`;
  if (includeContentType) headers["Content-Type"] = "application/json";
  return headers;
}

// ---------- Project History Loading ----------
async function loadHistory(project) {
  chatDiv.innerHTML = '<div class="message assistant"><em>Loading history...</em></div>';
  try {
    const res = await fetch(`/history/${encodeURIComponent(project)}`);
    const data = await res.json();
    chatDiv.innerHTML = "";
    if (data.history && data.history.length > 0) {
      data.history.forEach(msg => {
        if (msg.role === "user") {
          appendAndScroll(makeUserNode(msg.content));
        } else {
          const node = makeAssistantNode();
          node.innerHTML = `<strong>Assistant:</strong><br>${marked.parse(msg.content)}`;
          appendAndScroll(node);
        }
      });
      if (typeof hljs !== 'undefined') hljs.highlightAll();
    } else {
      chatDiv.innerHTML = '<div class="message assistant"><em>New project started. No history found.</em></div>';
    }
  } catch (err) {
    console.error("Error loading history:", err);
    chatDiv.innerHTML = '<div class="message assistant"><em>Error loading history for this project.</em></div>';
  }
}

projectSelect.addEventListener("change", () => {
  loadHistory(projectSelect.value);
});

function makeUserNode(text) {
  const node = document.createElement("div");
  node.className = "message user";
  node.innerHTML = `<strong>You:</strong><br>${marked.parse(text)}`;
  return node;
}

function makeAssistantNode() {
  const node = document.createElement("div");
  node.className = "message assistant";
  node.innerHTML = `<strong>Assistant:</strong><br><em>...</em>`;
  return node;
}

function appendAndScroll(node) {
  chatDiv.appendChild(node);
  chatDiv.scrollTop = chatDiv.scrollHeight;
}

async function refreshProjects() {
  if (!googleUserToken) return;
  const res = await fetch("/projects");
  const data = await res.json();
  const currentVal = projectSelect.value;
  projectSelect.innerHTML = "";
  data.forEach(p => {
    const opt = document.createElement("option");
    opt.value = p;
    opt.textContent = p;
    projectSelect.appendChild(opt);
  });
  if (data.includes(currentVal)) {
    projectSelect.value = currentVal;
  } else if (data.length > 0) {
    projectSelect.value = data[0];
    loadHistory(data[0]);
  }
}

addProjectBtn.addEventListener("click", async () => {
  const name = prompt("Project Name:");
  if (!name) return;
  await fetch(`/add_project`, {
    method: "POST",
    headers: await getHeaders(),
    body: JSON.stringify({ project: name })
  });
  await refreshProjects();
});

deleteProjectBtn.addEventListener("click", async () => {
  const p = projectSelect.value;
  if (!p || !confirm(`Delete project ${p}?`)) return;
  await fetch(`/delete_project`, {
    method: "POST",
    headers: await getHeaders(),
    body: JSON.stringify({ project: p })
  });
  await refreshProjects();
});

sendBtn.addEventListener("click", async () => {
  const text = promptInput.value.trim();
  if (!text) return;

  const project = projectSelect.value;
  const useWeb = document.getElementById("useWeb").checked;

  appendAndScroll(makeUserNode(text));
  promptInput.value = "";

  const assistantNode = makeAssistantNode();
  appendAndScroll(assistantNode);

  currentAbortController = new AbortController();

  try {
    let search_results = [];
    if (useWeb) {
      assistantNode.innerHTML = `<strong>Assistant:</strong><br><em>Searching web...</em>`;
      const sResp = await fetch("/search_web", {
        method: "POST",
        headers: await getHeaders(),
        body: JSON.stringify({ query: text }),
        signal: currentAbortController.signal
      });
      const sData = await sResp.json();
      search_results = sData.results || [];
    }

    const res = await fetch("/chat", {
      method: "POST",
      headers: await getHeaders(),
      body: JSON.stringify({ project, message: text, search_results }),
      signal: currentAbortController.signal
    });

    const data = await res.json();
    if (data.response) {
      assistantNode.innerHTML = `<strong>Assistant:</strong><br>${marked.parse(data.response)}`;
      if (typeof hljs !== 'undefined') hljs.highlightAll();
    } else if (data.error) {
      assistantNode.textContent = "Error: " + data.error;
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      assistantNode.innerHTML += "<br><em>[Stopped]</em>";
    } else {
      assistantNode.textContent = "Error: " + err.message;
    }
  } finally {
    currentAbortController = null;
    chatDiv.scrollTop = chatDiv.scrollHeight;
  }
});

clearBtn.addEventListener("click", () => {
  chatDiv.innerHTML = "";
});

async function uploadFile(project) {
  const fileInput = document.getElementById('fileInput');
  if (!fileInput.files.length) {
    alert("Please select a file first.");
    return;
  }
  const formData = new FormData();
  formData.append("file", fileInput.files[0]);

  try {
    const headers = await getHeaders(false); // browser sets multipart boundary
    const res = await fetch(`/upload_file/${project}`, {
      method: "POST",
      headers: headers,
      body: formData
    });
    const data = await res.json();
    if (data.status === "ok") {
      alert(`Uploaded: ${data.filename}`);
      fileInput.value = "";
    } else {
      alert(`Failed: ${data.error}`);
    }
  } catch (err) {
    alert("Error uploading file.");
  }
}

promptInput.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.key === "Enter") sendBtn.click();
});
