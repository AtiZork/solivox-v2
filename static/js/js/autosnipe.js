async function loadAutoSnipeSettings() {
  try {
    const token = localStorage.getItem('token');  // Get JWT token from localStorage
    console.log("token:", token);  // Debugging: Check the token value
    // const response = await fetch('/api/autosnipe');
    const response = await fetch('/api/autosnipe', {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${token}`,  // Include the JWT token in the Authorization header
        'Content-Type': 'application/json'
      }
    });
    if (!response.ok) throw new Error('Failed to load settings');
    const data = await response.json();

    for (const [key, value] of Object.entries(data)) {
      const input = document.getElementById(key);
      if (input) {
        if (input.type === 'checkbox') {
          input.checked = Boolean(value);
        } else {
          input.value = value;
        }
      }
    }
  } catch (error) {
    console.error('Error loading AutoSnipe settings:', error);
  }
}

document.getElementById("autosnipe-form").addEventListener("submit", async function (e) {
  e.preventDefault();
  const form = e.target;
  const formData = new FormData(form);
  const data = {};

  // Parse form data and handle different types (integers and floats)
  for (const [key, value] of formData.entries()) {
    if (key === "active") continue; // Handle active separately

    // Check if the value is supposed to be a float or integer
    if (key === "priority_fee" || key === "buy_amount" || key === "slippage") {
      // Parse these fields as floats
      data[key] = parseFloat(value);
    } else {
      // Parse other fields as integers
      data[key] = parseInt(value, 10);
    }
  }

  // Handle active checkbox explicitly
  const activeCheckbox = form.querySelector("#active");
  data.active = activeCheckbox ? activeCheckbox.checked : false;

  console.log("Payload:", JSON.stringify(data));  // Debugging: Check the final payload

  try {
    const token = localStorage.getItem('token');  // Get JWT token from localStorage
    const response = await fetch("/api/autosnipe", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${token}`  // Include the JWT token in the Authorization header
      },
      body: JSON.stringify(data),
    });
    // const response = await fetch("/api/autosnipe", {
    //   method: "POST",
    //   headers: { "Content-Type": "application/json" },
    //   body: JSON.stringify(data),
    // });

    if (response.ok) {
      alert("AutoSnipe settings saved!");

      // Reload latest data after save
      await loadAutoSnipeSettings();
    } else {
      alert("Failed to save AutoSnipe settings.");
    }
  } catch (err) {
    alert("Error saving AutoSnipe settings.");
    console.error(err);
  }
});

// Load settings on page load
document.addEventListener('DOMContentLoaded', loadAutoSnipeSettings);
