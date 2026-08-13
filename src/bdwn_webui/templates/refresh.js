// Spin the wheel while the browser fetches the refreshed page. The button is
// a plain link, so it still works with this script blocked: it just navigates
// without the local feedback.
// Kept free of the characters Jinja escapes, and hashed into the page CSP.
document.addEventListener("click", function (event) {
  var button = event.target.closest(".refresh");
  if (button) {
    button.classList.add("busy");
  }
});
// Coming back from the browser cache must not leave the wheel spinning.
window.addEventListener("pageshow", function () {
  var button = document.querySelector(".refresh");
  if (button) {
    button.classList.remove("busy");
  }
});
