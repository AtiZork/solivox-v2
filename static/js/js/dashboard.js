document.addEventListener("DOMContentLoaded", function () {
    let cachedConfigData = null; // Store API response

    function fetchConfigTypes() {
        fetch("/configurations") // Replace with your actual API endpoint
            .then(response => response.json())
            .then(data => {
                cachedConfigData = data; // Cache data

                const selectElement = document.getElementById("config-type");
                if (!selectElement) {
                    console.error("Select element with ID 'config-type' not found.");
                    return;
                }

                // Clear existing options
                selectElement.innerHTML = "<option value=''>Select an option</option>";

                // Append new options
                data.forEach(item => {
                    let option = document.createElement("option");
                    option.value = item.id;
                    option.textContent = item.name;
                    selectElement.appendChild(option);
                });
            })
            .catch(error => console.error("Error fetching config types:", error));
    }
    document.addEventListener("click", (e) => {
  // existing sell-enter-btn and time-btn logic ...

  if (e.target.matches(".sell-percent-btn")) {
    const tradeId = e.target.dataset.id;
    const percent = e.target.dataset.percent;

    fetch(`/sell_token/${tradeId}/`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ percent: parseFloat(percent) })
    })
    .then(res => res.json())
    .then(data => {
      alert(`✅ Sell ${percent}% triggered for Trade ID ${tradeId}`);
    })
    .catch(err => {
      console.error("❌ Sell failed:", err);
      alert(`❌ Failed to sell ${percent}% for Trade ID ${tradeId}`);
    });
  }
});

    function patchFields(config, type) {
        if (!config || !config.config_data) {
            console.error(`${type} config data is missing.`);
            return;
        }
        const nameFieldId = type === "LONG" ? "long_name" : type === "RAD" ? "name" : null;
        if (nameFieldId) {
            const nameField = document.getElementById(nameFieldId);
            if (nameField) {
                nameField.value = config.name || "";
            }
        }
        for (const key in config.config_data) {
            console.log('key', config.config_data);
            const inputField = document.getElementById(key);
            if (inputField) {
                if (inputField.type === "checkbox") {
                    inputField.checked = config.config_data[key] === true; // Handle checkboxes
                } else {
                    inputField.value = config.config_data[key];
                }
            }
        }
    }

    function loadModalData(modalType) {
        if (!cachedConfigData) {
            console.log("Config data is not available yet.");
            return;
        }

        const config = cachedConfigData.find(item => item.name === modalType);
        if (config) {
            if (modalType === "RAD") {
                patchFields(config, "RAD");
            } else if (modalType === "LONG") {
                patchFields(config, "LONG");
            }
        }
    }

    // Fetch configurations on page load
    fetchConfigTypes();

    // Load RAD data when the modal opens
    document.getElementById("radModal").addEventListener("show.bs.modal", function () {
        loadModalData("RAD");
    });

    // Load LONG data when the modal opens
    document.getElementById("longTradeModal").addEventListener("show.bs.modal", function () {
        loadModalData("LONG");
    });
});
