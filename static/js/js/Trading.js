document.addEventListener("DOMContentLoaded", async function () {
    const walletSelect = document.getElementById("wallet-key");
    try {
        const token = localStorage.getItem("token");
        const response = await fetch("/get_wallets", {
          method: "GET",
          headers: {
            "Authorization": `Bearer ${token}`,
            "Content-Type": "application/json"
          }
        });
        // const response = await fetch("/get_wallets");
        const wallets = await response.json();
        wallets?.wallets?.forEach(wallet => {
            const option = document.createElement("option");
            option.value = wallet.public_key;
            option.textContent = `${wallet.title} - Balance:  (${wallet.balance}) lamports`;
            walletSelect.appendChild(option);
        });
    } catch (error) {
        console.error("Error fetching wallets:", error);
    }
});
document.getElementById("TradingForm").addEventListener("submit", async function (event) {
    event.preventDefault();
    let walletKey = document.getElementById("wallet-key").value.trim();
    let tokenAddress = document.getElementById("token").value.trim();
    let amount = document.getElementById("tradamount").value.trim();
    let initialAmount = document.getElementById("initial-amount").value.trim();
    // const transactionType = document.getElementById("transaction-type").value;
    const transactionType = "BUY";
    const configType = document.getElementById("config-type").value;
    if (!initialAmount) {
        initialAmount = "0";
    }
    let isValid = true;
    if (walletKey === "") {
        document.getElementById("walletKeyError").classList.remove("d-none");
        isValid = false;
    } else {
        document.getElementById("walletKeyError").classList.add("d-none");
    }
    if (tokenAddress === "") {
        document.getElementById("tokenError").classList.remove("d-none");
        isValid = false;
    } else {
        document.getElementById("tokenError").classList.add("d-none");
    }
    if (amount === "") {
        document.getElementById("tradamountError").classList.remove("d-none");
        isValid = false;
    } else {
        document.getElementById("tradamountError").classList.add("d-none");
    }
    if (isValid) {
        const apiUrl = "/trade";
        const data = {
            to_pubkey: walletKey,
            amount: parseFloat(amount),
            initial_price: parseFloat(initialAmount),
            trade_type: transactionType,
            configuration_id: configType,
            token_address: tokenAddress,
        };
        try {
            const token = localStorage.getItem("token");
            console.log("token", token);
            const response = await fetch(apiUrl, {
                method: "POST",
                body: JSON.stringify(data),
                headers: {
            "Authorization": `Bearer ${token}`,
            "Content-Type": "application/json"
          },
            });
            const result = await response.json();
            if (response.ok) {
                alert(result.message);
            } else {
                alert(result.message);
            }
        } catch (error) {
            console.error("Error:", error);
        }
    }
});
document.getElementById("editTransactionForm").addEventListener("submit", async function (event) {
    event.preventDefault();
    if (!transactionId) {
        alert("Transaction ID is missing!");
        return;
    }

    let isValid = true;

    // Prepare requestData
    const requestData = {
        sell_100_at_30_percent_drop: parseFloat(document.getElementById("updateSellBuyPriceNegative").value),
        sell_100_after_100_percent_profit_drop: parseFloat(document.getElementById("updateSellAllPriceNegative").value),
        sell_at_200_percent_profit: parseFloat(document.getElementById("updateSellAfter200").value),
        sell_at_300_percent_profit: parseFloat(document.getElementById("updateSellAfter300").value),
        sell_at_500_percent_profit: parseFloat(document.getElementById("updateSellAfter500").value),
        sell_at_1000_percent_profit: parseFloat(document.getElementById("updateSellAfter1000").value),
        sell_at_2000_percent_profit: parseFloat(document.getElementById("updateSellAfter2000").value),
        sell_at_10000_percent_profit: parseFloat(document.getElementById("updateSellAfter10000").value),
        buy_gas_fee: parseFloat(document.getElementById("updateBuyGasSol").value),
        buy_slippage: parseFloat(document.getElementById("updateBuySlippage").value),
        sell_gas_fee: parseFloat(document.getElementById("updateSellGasSol").value),
        sell_slippage: parseFloat(document.getElementById("updateSellSlippage").value),
        buy_if_price_up: parseFloat(document.getElementById("buyIfPriceUp").value),
        buy_if_price_down: parseFloat(document.getElementById("buyIfPriceDown").value),
    };

    // Validate inputs
    Object.entries(requestData).forEach(([key, value]) => {
        const input = document.getElementById(key);
        if (input && !input.readOnly) {
            if (value === "" || isNaN(value)) {
                isValid = false;
                input.style.border = "2px solid red";
            } else {
                input.style.border = "";
            }
        }
    });

    if (!isValid) {
        alert("Please fill in all required fields correctly.");
        return;
    }
    fetch(`/trade/${transactionId}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(requestData),
        })
        .then(response => response.json())
        .then(data => {
            alert(data.message);
        })
        .catch(error => {
            console.error("Error submitting form:", error);
            alert("Failed to Update transaction.");
        });
});


// Function to fetch transactions from API
async function fetchTransactions() {
    const apiUrl = "/get_trades"; // Replace with actual API
    const token = localStorage.getItem("token"); // or wherever it's stored
    try {
        const response = await fetch(apiUrl, {
            method: "GET",
            headers: {
                "Authorization": `Bearer ${token}`,
                "Content-Type": "application/json"
            }
        });
        // const response = await fetch(apiUrl);
        if (!response.ok) {
            throw new Error(`API Error: ${response.statusText}`);
        }
        const transactions = await response.json();
        console.log(transactions);
        const container = document.getElementById("transactionCards");
        if (!container) {
            console.error("Container with ID 'transactionCards' not found!");
            return;
        }
        container.innerHTML = ""; // Clear previous transactions
        if (transactions.length === 0) {
            container.innerHTML = '<p style="text-align: center; font-size: 18px; color: gray;">No transactions found.</p>';
            return;
        }
        transactions.forEach(displayTransaction);
        // Attach event listeners after rendering
        document.querySelectorAll(".copy-btn").forEach(button => {
            button.addEventListener("click", () => {
                const tokenAddress = button.getAttribute("data-token");
                copyToClipboard(tokenAddress);
            });
        });

        // Attach event listeners for edit buttons
        document.querySelectorAll(".edit-btn").forEach(button => {
            button.addEventListener("click", () => {
                const value = button.getAttribute("data-token");
                openEditModal(value);
            });
        });
    } catch (error) {
        console.error("Error fetching transactions:", error);
    }
}

let transactionId = null;
// Function to open the edit modal
function openEditModal(value) {
    let transaction;
    transactionId = null;
    try {
        transaction = typeof value === "string" ? JSON.parse(value) : value;
    } catch (error) {
        console.error("Error parsing transaction data:", error);
        return;
    }
    const modal = document.getElementById("editModal");
    if (!modal) {
        console.error("Edit modal not found!");
        return;
    }
    transactionId = transaction.id;
    const isPending = transaction.trade_type === "BUY" && transaction.buy_token_if_price === true;
    if (isPending) {
        document.getElementById("buyIfFields").classList.remove("d-none");
        document.getElementById("buyIfPriceUp").value = transaction.buy_if_price_up || '';
        document.getElementById("buyIfPriceDown").value = transaction.buy_if_price_down || '';
    } else {
        document.getElementById("buyIfFields").classList.add("d-none");
    }
    // Set the token address inside the modal input field (or wherever required)
    document.getElementById("editTokenAddress").value = transaction.token_address || "";
    document.getElementById("editAmount").value = transaction.amount || "";
    document.getElementById("editInitialPrice").value = transaction.initial_price || "";
    document.getElementById("updateSellBuyPriceNegative").value = transaction.sell_100_at_30_percent_drop || 30;
    document.getElementById("updateSellAllPriceNegative").value = transaction.sell_100_after_100_percent_profit_drop || 30;
    document.getElementById("updateSellAfter200").value = transaction.sell_at_200_percent_profit || 10;
    document.getElementById("updateSellAfter300").value = transaction.sell_at_300_percent_profit || 10;
    document.getElementById("updateSellAfter500").value = transaction.sell_at_500_percent_profit || 10;
    document.getElementById("updateSellAfter1000").value = transaction.sell_at_1000_percent_profit || 10;
    document.getElementById("updateSellAfter2000").value = transaction.sell_at_2000_percent_profit || 10;
    document.getElementById("updateSellAfter10000").value = transaction.sell_at_10000_percent_profit || 10;
    document.getElementById("updateBuyGasSol").value = transaction.buy_gas_fee || 0.001;
    document.getElementById("updateBuySlippage").value = transaction.buy_slippage || 30;
    document.getElementById("updateSellGasSol").value = transaction.sell_gas_fee || 0.001;
    document.getElementById("updateSellSlippage").value = transaction.sell_slippage || 30;
    document.getElementById("buyIfPriceUp").value = transaction.buy_if_price_up || 0;
    document.getElementById("buyIfPriceDown").value = transaction.buy_if_price_down || 0;


    // Show the modal
    const modalInstance = new bootstrap.Modal(modal);
    modalInstance.show();
}

// Function to display a transaction in a card
function displayTransaction(transaction) {
    const container = document.getElementById("transactionCards");
    if (!container) return;

    // Create card element
    const card = document.createElement("div");
    card.classList.add("col-xl-4", "col-lg-6", "col-md-6", "col-12");
    // Determine status based on conditions
    const status = transaction.trade_type === 'BUY' && transaction.buy_token_if_price
        ? '<span class="badge bg-warning text-dark">Pending</span>'
        : '<span class="badge bg-success">Confirmed</span>';
    card.innerHTML = `
        <div class="card shadow-sm border">
            <div class="card-body">
                    <button class="edit-btn" data-token='${JSON.stringify(transaction)}'>
                        <i class="fas fa-edit"></i>
                    </button>
                
                <div class="d-flex align-items-center gap-2">
                    <p class="card-text mb-0"><strong>Name:</strong>
                        <span class="masked-address">${transaction.title}</span>
                    </p>
                    <button class="copy-btn" data-token="${transaction.title}">
                        <i class="fas fa-copy"></i>
                    </button>
                   
                </div>
                
                <div class="d-flex align-items-center justify-content-between">
                    <p class="card-text mb-0"><strong>Amount <small>(SOl)</small>:</strong> ${transaction.amount}</p>
                    <p class="card-text"><strong>Initial <small>($)</small>:</strong> ${transaction.initial_price}</p>
                </div>
                <div class="d-flex align-items-center gap-2">
                    <p class="card-text mb-0"><strong>Token Address:</strong>
                        <span class="masked-address">${maskTokenAddress(transaction.token_address)}</span>
                    </p>
                    <button class="copy-btn" data-token="${transaction.token_address}">
                        <i class="fas fa-copy"></i>
                    </button>
                </div>
                <p class="card-text"><strong>Created At:</strong> ${transaction.created_at}</p>
                <p class="card-text"><strong>Status:</strong> ${status}</p>
            </div>
        </div>
    `;

    // Append card to container
    container.appendChild(card);
}

// Function to mask the token address
function maskTokenAddress(address) {
    if (!address || address.length < 8) return address;
    return address.substring(0, 4) + "**************" + address.substring(address.length - 4);
}

// Function to copy the full token address to clipboard
function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
        // alert("Token Address Copied: " + text);
    }).catch(err => {
        console.error("Failed to copy: ", err);
    });
}

// Call the function to fetch and display transactions
fetchTransactions();
