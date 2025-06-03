document.addEventListener("DOMContentLoaded", function () {
    const defaultValues = {
        sell_at_30_percent_drop: 0.70,
        sell_at_200_percent_profit: 3.0,
        sell_at_300_percent_profit: 4.0,
        sell_at_400_percent_profit: 5.0,
        sell_at_1000_percent_profit: 11.0,
        sell_at_1500_percent_profit: 16.0,
        sell_all_if_profit_drops_33_percent: 0.67,
        rebuy_150_percent_after_sell: 1.5,
        rebuy_window: 90.0,
        sell_within_seconds: 45.0,
        buy_gas_fee: 0.01,
        sell_gas_fee: 0.01,
        buy_slippage: 0.50,
        sell_slippage: 0.50,
        buy_priority_fee: 0.01
    };

    const fieldsContainer = document.getElementById("fieldsContainer");
    const row = document.createElement("div");
    row.classList.add("row", "g-3"); // Bootstrap row with gap

    for (const key in defaultValues) {
        const col = document.createElement("div");
        col.classList.add("col-12", "col-md-6", "col-lg-6", "col-xl-6"); // 1 in xs, 2 in md, 3 in lg, 4 in xl
        col.innerHTML = `
            <label for="${key}" class="form-label">${key.replace(/_/g, ' ')}</label>
            <input type="number" class="form-control" id="${key}" name="${key}" value="${defaultValues[key]}" required>
        `;
        row.appendChild(col);
    }

    fieldsContainer.appendChild(row);

    document.getElementById("tradingForm").addEventListener("submit", function (event) {
        event.preventDefault();

        const formData = new FormData(event.target);
        const name = formData.get("name"); // Extract 'name' separately
        const config_data = {};

        // Convert form data to JSON and store other fields in 'config'
        for (const [key, value] of formData.entries()) {
            if (key !== "name") {
                config_data[key] = parseFloat(value);
            }
        }

        const requestData = {
            name,
            config_data
        };

        console.log("Submitting Data:", requestData);

        // Make a POST API call
        fetch("/configurations", {  // Replace with your API URL
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify(requestData)
        })
        .then(response => response.json())
        .then(data => {
            alert(data.message);
        })
        .catch(error => {
            console.error("Error submitting form:", error);
            alert("Failed to submit form.");
        });
    });
});
