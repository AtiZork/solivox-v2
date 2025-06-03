    // API endpoint (change this to your actual endpoint)
    const apiUrl = '/sell_token/';

    // Reusable API call function
    function callSellAPI({ sell_percent = null, sell_amount = null }) {
      const payload = { sell_percent, sell_amount };

      fetch(apiUrl, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(payload)
      })
      .then(res => res.json())
      .then(data => {
        console.log('Sell API Success:', data);
        alert('Sell action completed successfully.');
      })
      .catch(err => {
        console.error('Sell API Error:', err);
        alert('Error processing sell action.');
      });
    }



    window.addEventListener('DOMContentLoaded', () => {
      // Sell % buttons
      document.querySelectorAll('#sell-percent-section .btn').forEach(button => {
        button.addEventListener('click', () => {
          const percent = parseInt(button.textContent);
          if (!isNaN(percent)) {
            callSellAPI({ sell_percent: percent });
          }
        });
      });

      // Sell X button
      const sellEnterBtn = document.getElementById('sell-enter-btn');
      sellEnterBtn.addEventListener('click', () => {
        const inputVal = parseFloat(document.getElementById('sell-amount-input').value);
        if (isNaN(inputVal) || inputVal <= 0) {
          alert("Enter a valid Solana amount");
        } else {
          callSellAPI({ sell_amount: inputVal });
        }
      });
    });

    const modal = document.getElementById('tradeModal');
  const closeModalBtn = document.getElementById('closeModalBtn');
  const historyBtn = document.querySelector('.history-btn');
  const tbody = document.getElementById('trade-history-body');

  historyBtn.addEventListener('click', () => {
    tbody.innerHTML = '<tr><td colspan="5">Loading...</td></tr>';

    fetch('/api/trade_history')
      .then(res => res.json())
      .then(data => {
        tbody.innerHTML = '';
        if (Array.isArray(data) && data.length > 0) {
          data.forEach(trade => {
            const row = `<tr>
              <td>${trade.trade_type}</td>
              <td>${parseFloat(trade.amount).toFixed(4)}</td>
              <td>${parseFloat(trade.execution_price).toFixed(6)}</td>
              <td>${trade.timestamp}</td>
              <td>${trade.tx_id}</td>
            </tr>`;
            tbody.insertAdjacentHTML('beforeend', row);
          });
        } else {
          tbody.innerHTML = '<tr><td colspan="5">No trade history found.</td></tr>';
        }
        modal.style.display = 'block';
      })
      .catch(err => {
        console.error('Error fetching trade history:', err);
        tbody.innerHTML = '<tr><td colspan="5">Failed to load data.</td></tr>';
        modal.style.display = 'block';
      });
  });

  closeModalBtn.addEventListener('click', () => {
    modal.style.display = 'none';
  });

  window.addEventListener('click', event => {
    if (event.target === modal) {
      modal.style.display = 'none';
    }
  });

  const socket = io();
  const container = document.getElementById("trade-container");
   socket.on("tradeData", (dataList) => {
     container.innerHTML = ""; // Clear old data
     dataList.forEach((trade, index) => {
       const block = document.createElement("div");
       block.classList.add("trade-block");
       block.innerHTML = `
          <div class="token-header">
            <div>
              <div class="token-info">
                <span class="token-name">${trade.token_name}</span>
              </div>
              <div class="token-address">
                Token Address: <span class="address-value">${trade.token_address}</span>
                <button class="copy-btn" data-addr="${trade.token_address}">
                  <i class="fas fa-copy"></i>
                </button>
              </div>
              <div class="time-elapsed">Time elapsed: ${trade.data.time_elapsed || 'N/A'}</div>
            </div>
            <div>
              <div>Current Price(USD): ${trade.current_price}</div>
              <div>Current Market Cap(USD): ${trade.market_cap}</div>
              <div>Current Profit(%): ${trade.profit}</div>
              <div>Current Payout(SOL): ${trade.payout_sol}</div>
              <div>Current Payout(USD): ${trade.payout_usd}</div>
            </div>
          </div>
          <div class="chart-container">
            <canvas id="price-chart-${index}"></canvas>
          </div>
          <div class="quick-sell-buttons">
            <button class="time-btn" data-addr="${trade.id}" data-interval="free">Free</button>
            <button class="time-btn" data-addr="${trade.id}" data-interval="30">30 sec</button>
            <button class="time-btn" data-addr="${trade.id}" data-interval="60">1 min</button>
          </div>
          <div class="sell-section">
            <input type="number" class="sell-amount-input" placeholder="in solana" />
            <button class="sell-btn" data-addr="${trade.id}">Sell</button>
          </div>
        `;
       container.appendChild(block);
     });
   });

