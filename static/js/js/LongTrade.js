document.addEventListener("DOMContentLoaded", function () {
    // Default values for input fields
    const defaultValues = {
        sellBuyPriceNegative: 30,
        sellAllPriceNegative: 30,
        sellAfter200: 10,
        sellAfter300: 10,
        sellAfter500: 10,
        sellAfter1000: 10,
        sellAfter2000: 10,
        sellAfter10000: 10,
        buyGasSol: 0.01,
        buySlippage: 30,
        sellGasSol: 0.01,
        sellSlippage: 30
    };

    // Set default values in the form
    Object.keys(defaultValues).forEach(id => {
        const field = document.getElementById(id);
        if (field) field.value = defaultValues[id];
    });

    function validateForm() {
        let isValid = true;
        let firstEmptyField = null;

        const toggleSwitch = document.getElementById("toggleSwitch");
        const isToggleOn = toggleSwitch.checked; // Check if toggle is ON

        document.querySelectorAll("#BuyNowForm input:not([type='checkbox'])").forEach(input => {
            const value = input.value.trim();
            const inputId = input.id;
            const errorElement = document.getElementById(inputId + "Error");

            // Skip validation for upValue and downValue if toggle is OFF
            if (!isToggleOn && (inputId === "upValue" || inputId === "downValue")) {
                return;
            }

            // Ensure the field is not empty & is a valid number (for number fields)
            if (value === "" || (input.type === "number" && isNaN(parseFloat(value)))) {
                isValid = false;
                if (!firstEmptyField) firstEmptyField = input;
                input.style.border = "2px solid red"; // Highlight empty fields
                if (errorElement) errorElement.classList.remove("d-none"); // Show error message
            } else {
                input.style.border = ""; // Reset border if filled
                if (errorElement) errorElement.classList.add("d-none"); // Hide error message
            }
        });

        if (!isValid) {
            alert("Please fill in all required fields.");
            firstEmptyField?.focus(); // Focus on the first empty field
        }

        return isValid;
    }

    function submitForm(event, isBuyNow = false) {
        event.preventDefault();

        if (!validateForm()) return;
        const toggleSwitch = document.getElementById("toggleSwitch");
         const requestData = {
            to_pubkey: document.getElementById("wallet-key").value,
            token_address: document.getElementById("token").value,
            amount: parseFloat(document.getElementById("tradamount").value),
            sell_100_at_30_percent_drop: parseFloat(document.getElementById("sellBuyPriceNegative").value),
            sell_100_after_100_percent_profit_drop: parseFloat(document.getElementById("sellAllPriceNegative").value),
            sell_at_200_percent_profit: parseFloat(document.getElementById("sellAfter200").value),
            sell_at_300_percent_profit: parseFloat(document.getElementById("sellAfter300").value),
            sell_at_500_percent_profit: parseFloat(document.getElementById("sellAfter500").value),
            sell_at_1000_percent_profit: parseFloat(document.getElementById("sellAfter1000").value),
            sell_at_2000_percent_profit: parseFloat(document.getElementById("sellAfter2000").value),
            sell_at_10000_percent_profit: parseFloat(document.getElementById("sellAfter10000").value),
            buy_gas_fee: parseFloat(document.getElementById("buyGasSol").value),
            buy_slippage: parseFloat(document.getElementById("buySlippage").value),
            sell_gas_fee: parseFloat(document.getElementById("sellGasSol").value),
            sell_slippage: parseFloat(document.getElementById("sellSlippage").value),
            buy_if_price_up: parseFloat(document.getElementById("upValue").value) || 0,
            buy_if_price_down: parseFloat(document.getElementById("downValue").value) || 0,
            buy_token_if_price: toggleSwitch.checked || false,
            buy_now: !toggleSwitch.checked
        };


        // API Call
        const token = localStorage.getItem("token");
        fetch("/trade", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Authorization": `Bearer ${token}`
            },
            body: JSON.stringify(requestData),
        })
        // fetch("/trade", {
        //     method: "POST",
        //     headers: { "Content-Type": "application/json" },
        //     body: JSON.stringify(requestData),
        // })
        .then(response => response.json())
        .then(data => {
            alert(data.message);
        })
        .catch(error => {
            console.error("Error submitting form:", error);
            alert("Failed to submit long trade.");
        });
    }

    // Attach event listeners to form and buttons
    document.getElementById("BuyNowForm").addEventListener("submit", submitForm);

    const activateBtn = document.getElementById("Activate");
    const buyNowBtn = document.getElementById("BuyNow");

    if (activateBtn) activateBtn.addEventListener("click", (event) => submitForm(event, false));
    if (buyNowBtn) buyNowBtn.addEventListener("click", (event) => submitForm(event, true));
});

    // const defaultValues = {
    //     sellBuyPriceNegative: 0.70,
    //     sell_at_150_percent_profit: 2.5,
    //     sell_at_400_percent_profit: 5.0,
    //     sell_at_1000_percent_profit: 11.0,
    //     sell_at_1500_percent_profit: 16.0,
    //     sell_all_if_profit_drops_33_percent: 0.67,
    //     buy_slippage: 0.10,
    //     buy_gas_fee: 0.01,
    //     sell_slippage: 0.10,
    //     sell_gas_fee: 0.01,
    //     buy_if_price: 0,
    //     // buy_if_dip_percentage: 0,
    //     buy_now: false // Checkbox
    // };

    // const fieldsContainer = document.getElementById("fieldsContainerforLong");
    // const row = document.createElement("div");

    // row.classList.add("row", "g-3"); // Bootstrap grid with gap

    // for (const key in defaultValues) {
    //     const col = document.createElement("div");
    //     col.classList.add("col-md-6");
    //
    //     if (typeof defaultValues[key] === "boolean") {
    //         // Checkbox for buy_now
    //         col.innerHTML = `
    //             <div class="form-check">
    //                 <input type="checkbox" class="form-check-input" id="${key}" name="${key}" ${defaultValues[key] ? "checked" : ""}>
    //                 <label for="${key}" class="form-check-label">${key.replace(/_/g, ' ')}</label>
    //             </div>
    //         `;
    //     } else {
    //         // Numeric fields
    //         col.innerHTML = `
    //             <label for="${key}" class="form-label">${key.replace(/_/g, ' ')}</label>
    //             <input type="number" class="form-control" id="${key}" name="${key}" value="${defaultValues[key]}" required>
    //         `;
    //     }
    //
    //     row.appendChild(col);
    // }
//
//     fieldsContainer.appendChild(row);
//
//     document.getElementById("BuyNowForm").addEventListener("submit", function (event) {
//         event.preventDefault();
//
//         const formData = new FormData(event.target);
//         const name = formData.get("long_name"); // Extract 'long_name' separately
//         const config_data = {};
//
//         // Convert form data to JSON and store other fields in 'config'
//         for (const [key, value] of formData.entries()) {
//             if (key !== "long_name") {
//                 config_data[key] = value === "on" ? true : isNaN(value) ? value : parseFloat(value);
//             }
//         }
//
//         const requestData = {
//             name,
//             config_data
//         };
//
//         console.log("Submitting Data:", requestData);
//
//         // Make a POST API call
//         fetch("/configurations", {  // Replace with your API URL
//             method: "POST",
//             headers: {
//                 "Content-Type": "application/json"
//             },
//             body: JSON.stringify(requestData)
//         })
//         .then(response => response.json())
//         .then(data => {
//
//             alert(data.message);
//         })
//         .catch(error => {
//             console.error("Error submitting form:", error);
//             alert("Failed to submit long trade.");
//         });
//     });
// });
