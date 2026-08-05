document.addEventListener("DOMContentLoaded", () => {
  const appPorts = {
    "/": 5050,
    "/piezo": 5001,
    "/stage": 5003,
    "/power": 5002,
    "/hr4000": 5004,
    "/avantes": 5005,
  };

  document.querySelectorAll(".app-nav a").forEach((link) => {
    const port = appPorts[link.getAttribute("href")];
    if (port) {
      link.href = `${window.location.protocol}//${window.location.hostname}:${port}`;
    }
  });
});