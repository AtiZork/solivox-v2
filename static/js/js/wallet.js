



document.addEventListener("DOMContentLoaded", function () {
    // Open & Close Drawer
    document.getElementById("openDrawer").addEventListener("click", function () {
        document.getElementById("drawer").classList.add("show");
    });

    document.getElementById("closeDrawer").addEventListener("click", function () {
        document.getElementById("drawer").classList.remove("show");
    });
 const waletNameInput = document.getElementById("waletName");
  const waletNameError = document.getElementById("waletNameError");
    const publicKeyInput = document.getElementById("publicKey");
    const amountInput = document.getElementById("amount");
    const publicKeyError = document.getElementById("publicKeyError");
    const amountError = document.getElementById("amountError");

    const attach_wallet_privateInput = document.getElementById("attach-wallet-privateKey");
    const attach_wallet_publicKeyInput = document.getElementById("attach-wallet-publicKey");
    const attach_wallet_privateInputError = document.getElementById("attach-wallet-privateKeyError");
    const attach_wallet_publicKeyInputError = document.getElementById("attach-wallet-publicKeyError");
    const recoveryPhraseInput = document.getElementById("recovery-phrase-key");
    const recoveryPhraseError = document.getElementById("recovery-phrase-keyError");
    const checkwalletInput = document.getElementById("checkwallet-key");
    const checkwalletInputError = document.getElementById("checkwallet-keyError");
    // Function to remove error when user types
    function removeError(inputElement, errorElement) {
        inputElement.addEventListener("input", function () {
            if (inputElement.value.trim() !== "") {
                errorElement.classList.add("d-none");
                inputElement.classList.remove("border", "border-danger");
            }
        });
    }
    removeError(waletNameInput, waletNameError);
    removeError(publicKeyInput, publicKeyError);
    removeError(amountInput, amountError);
    removeError(attach_wallet_privateInput, attach_wallet_privateInputError);
    removeError(attach_wallet_publicKeyInput, attach_wallet_publicKeyInputError);
    removeError(recoveryPhraseInput, recoveryPhraseError);
    removeError(checkwalletInput, checkwalletInputError);
    // Validate Recovery Phrase
 



document.getElementById("createwalletForm").addEventListener("submit",  async  function (event) {
        event.preventDefault();

        let valid = true;
        let waletNameInputValue = waletNameInput.value.trim();


        if (waletNameInputValue === "") {
            waletNameError.classList.remove("d-none");
            waletNameInput.classList.add("border", "border-danger");
            valid = false;
        }



        if (valid) {


               const apiUrl = "/create_wallet"; // Replace with actual API

        // API Data (Customize based on your needs)
            const data ={
                title:waletNameInputValue,
            }


        try {
            // Send POST Request
            const token = localStorage.getItem("token");  // Get JWT token
            console.log("Token:", token);  // Debugging: Check if token is retrieved correctly
            const response = await fetch(apiUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${token}`  // ✅ Send token in header
                },
                body: JSON.stringify(data),
            });
            // const response = await fetch(apiUrl, {
            //     method: "POST",
            //     headers: {
            //         "Content-Type": "application/json",
            //     },
            //     body: JSON.stringify(data),
            // });



            const result = await response.json();

            if (response.ok) {
               alert(result.message);
               waletNameInput.value = "";
               fetchWallets()
            } else {
                alert("Failed to create wallet: " + result.message);
            }
        } catch (error) {
            console.error("Error:", error);

        }



        }
    });









    document.getElementById("checkwalletForm").addEventListener("submit",  async  function (event) {
        event.preventDefault(); 
    
        let valid = true;
        let checkwalletInputValue = checkwalletInput.value.trim();
    
    
        if (checkwalletInputValue === "") {
            checkwalletInputError.classList.remove("d-none");
            checkwalletInput.classList.add("border", "border-danger");
            valid = false;
        }
    
       
    
        if (valid) {
            console.log("recoveryPhraseInput", checkwalletInputValue);
           
               const apiUrl = "/check_balance"; // Replace with actual API

        // API Data (Customize based on your needs)
            const data ={
                public_key:checkwalletInputValue,
            }


        try {
            // Send POST Request
            const response = await fetch(apiUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify(data),
            });



            const result = await response.json();

            if (response.ok) {


                console.log("Wallet Data:", result.balance);
                 alert("balance is" + "  " + result.balance);
                 checkwalletInput.value = "";
               
            } else {
                alert("Failed to create wallet: " + result.message);
            }
        } catch (error) {
            console.error("Error:", error);
            alert("An error occurred while creating the wallet.");
        }



        }
    });

    // Form Validation - Found Wallet
    document.getElementById("FoundwalletForm").addEventListener("submit", async  function (event) {
        event.preventDefault(); 

        let valid = true;
        let publicKeyValue = publicKeyInput.value.trim();
        let amountValue = amountInput.value.trim();

        if (publicKeyValue === "") {
            publicKeyError.classList.remove("d-none");
            publicKeyInput.classList.add("border", "border-danger");
            valid = false;
        }

        if (amountValue === "") {
            amountError.classList.remove("d-none");
            amountInput.classList.add("border", "border-danger");
            valid = false;
        }

        if (valid) {

               const apiUrl = "/fund_wallet"; // Replace with actual API

        // API Data (Customize based on your needs)
            const data ={
                public_key:publicKeyValue,
                amount: parseFloat(amountValue)
            }


        try {
            // Send POST Request
            const response = await fetch(apiUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify(data),
            });



            const result = await response.json();

            if (response.ok) {



                 alert(result.message);
                 publicKeyInput.value = "";
                 amountInput.value = "";
                 fetchWallets()
            } else {
                alert("Failed to create wallet: " + result.message);
            }
        } catch (error) {
            console.error("Error:", error);
            alert("An error occurred while creating the wallet.");
        }
        }
    });

    // Form Validation - Attached Wallet
    document.getElementById("AttachedwalletForm").addEventListener("submit",  async  function (event) {
        event.preventDefault(); 

        let valid = true;
        let privateKeyValue = attach_wallet_privateInput.value.trim();
        let publicKeyValue = attach_wallet_publicKeyInput.value.trim();

        if (privateKeyValue === "") {
            attach_wallet_privateInputError.classList.remove("d-none");
            attach_wallet_privateInput.classList.add("border", "border-danger");
            valid = false;
        }

        if (publicKeyValue === "") {
            attach_wallet_publicKeyInputError.classList.remove("d-none");
            attach_wallet_publicKeyInput.classList.add("border", "border-danger");
            valid = false;
        }

        if (valid) {
            console.log("Private Key:", privateKeyValue);
                   const apiUrl = "/attach_wallet"; // Replace with actual API

        // API Data (Customize based on your needs)
            const data ={
                public_key:publicKeyValue,
                private_key:privateKeyValue
            }


        try {
            // Send POST Request
            const token = localStorage.getItem("token");
            const response = await fetch(apiUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${token}`
                },
                body: JSON.stringify(data),
            });



            const result = await response.json();

            if (response.ok) {



                 alert(result.message);
                 attach_wallet_privateInput.value = "";
                  attach_wallet_publicKeyInput.value = "";
            } else {
                alert("Failed to create wallet: " + result.message);
            }
        } catch (error) {
            console.error("Error:", error);
            alert("An error occurred while creating the wallet.");
        }
        }
    });

    // Form Validation - Recover Wallet
 // Validate "Recover Wallet" Form
 document.getElementById("RecoverwalletForm").addEventListener("submit",  async function (event) {
    event.preventDefault(); 

    let valid = true;
    let recoveryPhraseInputValue = recoveryPhraseInput.value.trim();


    if (recoveryPhraseInputValue === "") {
        recoveryPhraseError.classList.remove("d-none");
        recoveryPhraseInput.classList.add("border", "border-danger");
        valid = false;
    }

   

    if (valid) {
        console.log("recoveryPhraseInput", recoveryPhraseInputValue);
       
               const apiUrl = "/recover_wallet"; // Replace with actual API

        // API Data (Customize based on your needs)
            const data ={
                recovery_phrase:recoveryPhraseInputValue,

            }


        try {
            // Send POST Request
            const response = await fetch(apiUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify(data),
            });



            const result = await response.json();

            if (response.ok) {



                 alert(result.message);
                 recoveryPhraseInput.value = "";
                  
            } else {
                alert(result.message);
            }
        } catch (error) {
            console.error("Error:", error);
            alert("An error occurred while creating the wallet.");
        }
    }
});

const walletContainer = document.getElementById("walletContainer");

// async function fetchWallets() {
//     try {
//         const token = localStorage.getItem("token");
//         const response = await fetch("/get_wallets", {
//           method: "GET",
//           headers: {
//             "Authorization": `Bearer ${token}`,
//             "Content-Type": "application/json"
//           }
//         });
//         // const response = await fetch("/get_wallets");
//         const data = await response.json();
//
//         if (!data?.wallets || !Array.isArray(data.wallets)) {
//             throw new Error("Invalid response format");
//         }
//
//         const colors = ["bg-primary", "bg-success", "bg-warning", "bg-danger", "bg-info"];
//         let walletCardsHTML = '<div class="row">'; // Start Bootstrap row
//
//         data.wallets.forEach((wallet, index) => {
//             const bgColor = colors[index % colors.length];
//             const formattedBalance = wallet.balance ? `$${wallet.balance.toLocaleString()}` : "$0.00";
//
//             walletCardsHTML += `
//                 <div class="col-md-4  col-sm-6  mb-4"> <!-- 4 cards per row on md+, 2 per row on sm -->
//                     <div class="wallet-card ${bgColor} p-3 rounded shadow">
//                         <div class="text-white">
//                             <p class="fs-14 mb-0 font-w100">${wallet.title || "No Title"}</p>
//                             <span>${formattedBalance}</span>
//                         </div>
//                         <div class="wallet-footer mt-3">
//                             <div class="fs-14">${wallet.public_key.slice(0, 6)}**********${wallet.public_key.slice(-4)}</div>
//                             <div><button class="address-copy-btn" onclick="copyWalletAddress('${wallet.public_key}')">Copy</button></div>
//
//                         </div>
//                     </div>
//                 </div>
//             `;
//         });
//
//         walletCardsHTML += '</div>'; // Close Bootstrap row
//         walletContainer.innerHTML = walletCardsHTML; // Update DOM once
//
//     } catch (error) {
//         console.error("Error fetching wallets:", error);
//     }
// }
//
//
// fetchWallets();
async function fetchWallets() {
    try {
        const token = localStorage.getItem("token");
        const response = await fetch("/get_wallets", {
            method: "GET",
            headers: {
                "Authorization": `Bearer ${token}`,
                "Content-Type": "application/json"
            }
        });

        const data = await response.json();

        if (!data?.wallets || !Array.isArray(data.wallets)) {
            throw new Error("Invalid response format");
        }

        const colors = ["bg-primary", "bg-success", "bg-warning", "bg-danger", "bg-info"];
        let walletCardsHTML = '<div class="row">'; // Start Bootstrap row

        data.wallets.forEach((wallet, index) => {
            const bgColor = colors[index % colors.length];
            const formattedBalance = wallet.balance ? `$${wallet.balance.toLocaleString()}` : "$0.00";

            walletCardsHTML += `
                <div class="col-md-4 col-sm-6 mb-4"> <!-- 4 cards per row on md+, 2 per row on sm -->
                    <div class="wallet-card ${bgColor} p-3 rounded shadow">
                        <div class="text-white">
                            <p class="fs-14 mb-0 font-w100">${wallet.title || "No Title"}</p>
                            <span>${formattedBalance}</span>
                        </div>
                        <div class="wallet-footer mt-3">
                            <div class="fs-14">${wallet.public_key.slice(0, 6)}**********${wallet.public_key.slice(-4)}</div>
                            <div>
                               <button class="address-copy-btn" onclick="(async () => {
    const walletAddress = '${wallet?.public_key}'; // Get the wallet address
    if (!walletAddress) {
        alert('Wallet address is missing');
        return;
    }

    try {
        // Create a temporary input element to use the execCommand for copying
        const input = document.createElement('input');
        input.value = walletAddress;
        document.body.appendChild(input);
        input.select(); // Select the text in the input
        const successful = document.execCommand('copy'); // Execute the copy command

        if (successful) {
            alert('Wallet address copied: ' + walletAddress);
        } else {
            alert('Failed to copy wallet address.');
        }

        document.body.removeChild(input); // Remove the temporary input after copying
    } catch (error) {
        console.error('Failed to copy wallet address:', error);
        alert('Failed to copy the wallet address.');
    }
})()">Copy</button>
                            </div>
                        </div>
                    </div>
                </div>
            `;
        });

        walletCardsHTML += '</div>'; // Close Bootstrap row
        walletContainer.innerHTML = walletCardsHTML; // Update DOM once

    } catch (error) {
        console.error("Error fetching wallets:", error);
    }
}

// Call function to load wallets
fetchWallets();


});
