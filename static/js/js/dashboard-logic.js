function formatNumber(value) {
  if (value >= 1_000_000_000) return (value / 1_000_000_000).toFixed(1) + "B";
  if (value >= 1_000_000) return (value / 1_000_000).toFixed(1) + "M";
  if (value >= 1_000) return (value / 1_000).toFixed(1) + "K";
  return value.toFixed(2);
}
  let tradeId = null;
  // const socket = io("http://localhost:8000");
  const socket = io(`${window.location.protocol}//${window.location.host}`);
  const wrapper = document.getElementById("trade-wrapper");
  const chartMap = {};

  socket.on("connect", () => {
    console.log("✅ Connected");
    socket.emit("subscribe_trades");
  });

  socket.on("trades_data", (trades) => {
    console.log('trade Data', trades);
    wrapper.innerHTML = ""; // Clear old

    trades.forEach((trade, index) => {
      const tokenId = trade.token_address;
      const chartId = `price-chart-${index}`;
      const status = (trade.trade_type === 'BUY' && trade.buy_token_if_price === true) ? 'Pending' : 'Confirmed';

      const block = document.createElement("div");
      // block.className = "container";
      block.className = "trade-card";
      block.setAttribute("data-token", tokenId);
      const dataTokenStr = encodeURIComponent(JSON.stringify(trade));

      block.innerHTML = `
<!--        <div class="header-dashboard">-->
<!--          <p style="font-size: 2.25rem; font-weight: 500">TRADE MONITOR</p>-->
<!--          <div class="status">🟢 Live</div>-->
<!--        </div>-->

        <div class="token-header">
          <div class="d-flex justify-content-between">
            <div>
              <div class="token-info">
                <div><span class="token-name">${trade.token_name}</span></div>
              </div>
              <div class="d-flex">Token Address: <span class="address-value">${tokenId}</span>
                <button class="copy-btn" style="margin-left: 10px">
                  <i class="fas fa-copy"></i>
                </button>
              </div>
              <div class="time-elapsed">
                Time elapsed: ${trade.created_at || 'N/A'} <br>
                Status: ${status}
              </div>
            </div>
            <div>
              <div class="current-price">Current Price(USD): ${trade.current_price}</div>
<!--              <div>Current Market Cap(USD): ${trade.market_cap}</div>-->
              <div>Current Market Cap(USD): ${formatNumber(trade.market_cap)}</div>
              <div>Current Profit(%): ${trade.profit}</div>
              <div>Current Payout(SOL): ${trade.payout_sol}</div>
              <div>Current Payout(USD): ${trade.payout_usd}</div>
            </div>
          </div>
        </div>

        <div class="chart-container">
          <canvas id="${chartId}"></canvas>
        </div>
        <div  class="d-flex justify-content-between gap-2">
        <div class="quick-sell-buttons">
          <button class="time-btn active" data-addr="${tokenId}" data-interval="free">Free</button>
          <button class="time-btn sell-enter-btn" data-addr="${tokenId}" data-interval="30">30 sec</button>
          <button class="time-btn sell-enter-btn" data-addr="${tokenId}" data-interval="60">1 min</button>
          <button class="time-btn sell-enter-btn" data-addr="${tokenId}" data-interval="900">15 min</button>
          <button class="time-btn sell-enter-btn" data-addr="${tokenId}" data-interval="3600">1 hour</button>
          <button class="time-btn sell-enter-btn" data-addr="${tokenId}" data-interval="86400">1 day</button>
        </div>
               <div>
        <button class="btn btn-outline-light btn-sm toggle-auto-sell-btn"
        data-id="${trade.id}"
        data-state="${trade.auto_sell}">
        ${trade.auto_sell ? '🟢 Auto Sell ON' : '⚪ Auto Sell OFF'}
        </button>
        <button class="btn btn-sm btn-primary open-config-modal-btn px-3"
          style="background-color: #4a3aff; border: none; color: white;"
          data-token='${encodeURIComponent(JSON.stringify(trade))}'>
          ⚙️ Change
        </button>


         </div>
         </div>

        <div class="sell-section" id="sell-percent-section">
          <p class="mt-2" style="font-size: 1.25rem; font-weight: 500"> Sell %</p>
          <button class="btn btn-sm btn-primary sell-percentage-btn" data-id="${trade.id}" data-percent="10">10</button>
          <button class="btn btn-sm btn-primary sell-percentage-btn" data-id="${trade.id}" data-percent="25">25</button>
          <button class="btn btn-sm btn-primary sell-percentage-btn" data-id="${trade.id}" data-percent="50">50</button>
          <button class="btn btn-sm btn-primary sell-percentage-btn" data-id="${trade.id}" data-percent="80">80</button>
          <button class="btn btn-sm btn-primary sell-percentage-btn" data-id="${trade.id}" data-percent="100">100</button>
        </div>
<div class="d-flex">
<div class="col-6">
<div class="sell-section">
          <p class="mt-2" style="font-size: 1.25rem; font-weight: 500; width: 15%"> Sell X</p>
          <div class="input-group" style="width: 30%; margin-top: 7px">
            <label>
              <input type="number" placeholder="in solana" class="form-control sell-amount-input">
            </label>
          </div>
          <button class="btn btn-sm btn-primary sell-enter-btn" data-id="${trade.id}">Confirm</button>
        </div>

</div>
<div class="col-6">
        <div class="sell-section">
          <p class="mt-2" style="font-size: 1.25rem; font-weight: 500; width: 15%"> Buy X</p>
          <div class="input-group" style="width: 30%; margin-top: 7px">
            <label>
              <input type="number" placeholder="in solana" class="form-control buy-amount-input">
            </label>
          </div>
          <button class="btn btn-sm btn-primary buy-enter-btn" data-id="${trade.id}">Confirm</button>
        </div>
</div>
</div>

<button type="button" class="btn btn-primary history-btn" data-id="${trade.id}">
  History
</button>

<div class="modal fade" id="tradeModal-${trade.id}" tabindex="-1" aria-labelledby="tradeModalLabel-${trade.id}" aria-hidden="true">
  <div class="modal-dialog modal-xl modal-dialog-scrollable">
    <div class="modal-content bg-dark text-white">
      <div class="modal-header">
        <h5 class="modal-title" id="tradeModalLabel-${trade.id}">Trade History</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
      </div>
      <div class="modal-body">
        <table class="table table-dark table-bordered table-striped table-hover">
          <thead>
            <tr>
              <th>Trade Type</th>
              <th>Amount</th>
              <th>Execution Price</th>
              <th>Timestamp</th>
              <th>Tx ID</th>
            </tr>
          </thead>
          <tbody id="trade-history-body-${trade.id}"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

                <div class="modal fade" id="editModal" tabindex="-1" aria-labelledby="editModalLabel"
                     aria-hidden="true">
                    <div class="modal-dialog modal-lg">
                        <div class="modal-content">
                            <div class="modal-header">
                                <h5 class="modal-title" id="editModalLabel">Edit Transaction</h5>
                                <button type="button" class="btn-close" data-bs-dismiss="modal"
                                        aria-label="Close"></button>
                            </div>
                            <div class="modal-body">
                                    <div class="mb-3">
                                        <label for="editTokenAddress" class="form-label" style="color: black">Token Address</label>
                                        <input type="text" class="form-control disableInput" id="editTokenAddress">
                                    </div>
                                    <div class="mb-3">
                                        <label for="editAmount" class="form-label" style="color: black">Amount</label>
                                        <input type="number" class="form-control disableInput" id="editAmount"
                                               placeholder="Enter amount">
                                    </div>
                                    <div class="mb-3">
                                        <label for="editInitialPrice" class="form-label" style="color: black">Initial Price</label>
                                        <input type="text" class="form-control disableInput" id="editInitialPrice"
                                               placeholder="Enter price">
                                    </div>
                                    <div class="row">
                                        <div class="col-xl-6 col-sm-6">
                                            <div class="mb-2">
                                                <label for="updateSellBuyPriceNegative" class="form-label" style="color: black">Sell all if buy price -ve</label>
                                                <div class="input-group">
                                                    <input type="number" id="updateSellBuyPriceNegative" class="form-control" step="any">
                                                    <span class="input-group-text">%</span>
                                                </div>
                                                <small class="text-danger d-none error-message" id="updateSellBuyPriceNegativeError">Amount is required!</small>
                                            </div>
                                        </div>
                                        <div class="col-xl-6 col-sm-6">
                                            <div class="mb-2">
                                                <label for="updateSellAllPriceNegative" class="form-label" style="color: black">Sell all after +100% if total profit -ve</label>
                                                <div class="input-group">
                                                    <input type="number" id="updateSellAllPriceNegative" class="form-control" step="any">
                                                    <span class="input-group-text">%</span>
                                                </div>
                                                <small class="text-danger d-none error-message" id="updateSellAllPriceNegativeError">Amount is required!</small>
                                            </div>
                                        </div>
                                        <div class="col-xl-6 col-sm-6">
                                            <div class="mb-2">
                                                <label for="updateSellAfter200" class="form-label" style="color: black">Sell after 200% profit</label>
                                                <div class="input-group">
                                                    <input type="number" id="updateSellAfter200" class="form-control" step="any">
                                                    <span class="input-group-text">%</span>
                                                </div>
                                                <small class="text-danger d-none error-message" id="updateSellAfter200Error">Amount is required!</small>
                                            </div>
                                        </div>
                                        <div class="col-xl-6 col-sm-6">
                                            <div class="mb-2">
                                                <label for="updateSellAfter300" class="form-label" style="color: black">Sell after 300% profit</label>
                                                <div class="input-group">
                                                    <input type="number" id="updateSellAfter300" class="form-control" step="any">
                                                    <span class="input-group-text">%</span>
                                                </div>
                                                <small class="text-danger d-none error-message" id="updateSellAfter300Error">Amount is required!</small>
                                            </div>
                                        </div>
                                        <div class="col-xl-6 col-sm-6">
                                            <div class="mb-2">
                                                <label for="updateSellAfter500" class="form-label" style="color: black">Sell after 500% profit</label>
                                                <div class="input-group">
                                                    <input type="number" id="updateSellAfter500" class="form-control" step="any">
                                                    <span class="input-group-text">%</span>
                                                </div>
                                                <small class="text-danger d-none error-message" id="updateSellAfter500Error">Amount is required!</small>
                                            </div>
                                        </div>
                                         <div class="col-xl-6 col-sm-6">
                                            <div class="mb-2">
                                                <label for="updateSellAfter1000" class="form-label" style="color: black">Sell after 1000% profit</label>
                                                <div class="input-group">
                                                    <input type="number" id="updateSellAfter1000" class="form-control" step="any">
                                                    <span class="input-group-text">%</span>
                                                </div>
                                                <small class="text-danger d-none error-message" id="updateSellAfter1000Error">Amount is required!</small>
                                            </div>
                                        </div>
                                        <div class="col-xl-6 col-sm-6">
                                            <div class="mb-2">
                                                <label for="updateSellAfter2000" class="form-label" style="color: black">Sell after 2000% profit</label>
                                                <div class="input-group">
                                                    <input type="number" id="updateSellAfter2000" class="form-control" step="any">
                                                    <span class="input-group-text">%</span>
                                                </div>
                                                <small class="text-danger d-none error-message" id="updateSellAfter2000Error">Amount is required!</small>
                                            </div>
                                        </div>
                                        <div class="col-xl-6 col-sm-6">
                                            <div class="mb-2">
                                                <label for="updateSellAfter10000" class="form-label" style="color: black">Sell after 10000% profit</label>
                                                <div class="input-group">
                                                    <input type="number" id="updateSellAfter10000" class="form-control" step="any">
                                                    <span class="input-group-text">%</span>
                                                </div>
                                                <small class="text-danger d-none error-message" id="updateSellAfter10000Error">Amount is required!</small>
                                            </div>
                                        </div>
                                        <div class="col-xl-6 col-sm-6">
                                            <div class="mb-2">
                                                <label for="updateSellGasSol" class="form-label" style="color: black">Buy Gas Fee</label>
                                                <div class="input-group">
                                                    <input type="number" id="updateBuyGasSol" class="form-control" step="any">
                                                    <span class="input-group-text">Sol</span>
                                                </div>
                                                <small class="text-danger d-none error-message" id="updateBuyGasSolError">Amount is required!</small>
                                            </div>
                                        </div>
                                        <div class="col-xl-6 col-sm-6">
                                            <div class="mb-2">
                                                <label for="updateBuySlippage" class="form-label" style="color: black">Buy Slippage</label>
                                                <div class="input-group">
                                                    <input type="number" id="updateBuySlippage" class="form-control" step="any">
                                                    <span class="input-group-text">%</span>
                                                </div>
                                                <small class="text-danger d-none error-message" id="updateBuySlippageError">Amount is required!</small>
                                            </div>
                                        </div>
                                        <div class="col-xl-6 col-sm-6">
                                            <div class="mb-2">
                                                <label for="updateSellGasSol" class="form-label" style="color: black">Sell Gas Fee</label>
                                                <div class="input-group">
                                                    <input type="number" id="updateSellGasSol" step="any" class="form-control">
                                                    <span class="input-group-text">Sol</span>
                                                </div>
                                                <small class="text-danger d-none error-message" id="updateSellGasSolError">Amount is required!</small>
                                            </div>
                                        </div>
                                        <div class="col-xl-6 col-sm-6">
                                            <div class="mb-2">
                                                <label for="updateSellSlippage" class="form-label" style="color: black">Sell Slippage</label>
                                                <div class="input-group">
                                                    <input type="number" id="updateSellSlippage" class="form-control" step="any">
                                                    <span class="input-group-text">%</span>
                                                </div>
                                                <small class="text-danger d-none error-message" id="updateSellSlippageError">Amount is required!</small>
                                            </div>
                                        </div>
                                        <div id="buyIfFields" class="d-none">
                                            <div class="row">
                                                <div class="form-group col-6">
                                                    <label for="buyIfPriceUp" class="form-label" style="color: black">Buy If Price Up</label>
                                                    <input type="number" class="form-control" step="any" id="buyIfPriceUp" />
                                                </div>
                                                <div class="form-group col-6">
                                                    <label for="buyIfPriceDown" class="form-label" style="color: black">Buy If Price Down</label>
                                                    <input type="number" class="form-control" step="any" id="buyIfPriceDown" />
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                    <button type="submit" id="saveChangesBtn" class="btn btn-primary">Save Changes</button>
                            </div>
                        </div>
                    </div>
                </div>




      `;


      document.addEventListener("click", function (e) {
        if (e.target.classList.contains("open-config-modal-btn")) {
          const rawValue = decodeURIComponent(e.target.getAttribute("data-token"));
          openEditModal(rawValue);
        }
      });

    function openEditModal(value) {
      let trade;
      try {
        trade = typeof value === "string" ? JSON.parse(value) : value;
      } catch (error) {
        console.error("Error parsing transaction data:", error);
        return;
      }
      tradeId = trade.id;
      const modal = document.getElementById("editModal");
      if (!modal) {
        console.error("Edit modal not found!");
        return;
      }
      const isPending = trade.trade_type === "BUY" && trade.buy_token_if_price === true;
      if (isPending) {
          document.getElementById("buyIfFields").classList.remove("d-none");
          document.getElementById("buyIfPriceUp").value = trade.buy_if_price_up || '';
          document.getElementById("buyIfPriceDown").value = trade.buy_if_price_down || '';
      } else {
          document.getElementById("buyIfFields").classList.add("d-none");
      }
      document.getElementById("editTokenAddress").value = trade.token_address || "";
      document.getElementById("editAmount").value = trade.amount || "";
      document.getElementById("editInitialPrice").value = trade.initial_price || "";
      document.getElementById("updateSellBuyPriceNegative").value = trade.sell_100_at_30_percent_drop || 30;
      document.getElementById("updateSellAllPriceNegative").value = trade.sell_100_after_100_percent_profit_drop || 30;
      document.getElementById("updateSellAfter200").value = trade.sell_at_200_percent_profit || 10;
      document.getElementById("updateSellAfter300").value = trade.sell_at_300_percent_profit || 10;
      document.getElementById("updateSellAfter500").value = trade.sell_at_500_percent_profit || 10;
      document.getElementById("updateSellAfter1000").value = trade.sell_at_1000_percent_profit || 10;
      document.getElementById("updateSellAfter2000").value = trade.sell_at_2000_percent_profit || 10;
      document.getElementById("updateSellAfter10000").value = trade.sell_at_10000_percent_profit || 10;
      document.getElementById("updateBuyGasSol").value = trade.buy_gas_fee || 0.001;
      document.getElementById("updateBuySlippage").value = trade.buy_slippage || 30;
      document.getElementById("updateSellGasSol").value = trade.sell_gas_fee || 0.001;
      document.getElementById("updateSellSlippage").value = trade.sell_slippage || 30;
      document.getElementById("buyIfPriceUp").value = trade.buy_if_price_up || 0;
      document.getElementById("buyIfPriceDown").value = trade.buy_if_price_down || 0;

      const modalInstance = new bootstrap.Modal(modal);
      modalInstance.show();
    }

    document.addEventListener("click", function (e) {
  if (e.target && e.target.id === "saveChangesBtn") {
    if (!tradeId) {
      alert("Missing trade ID.");
      return;
    }

    const requestData = {
      token_address: document.getElementById("editTokenAddress").value,
      amount: parseFloat(document.getElementById("editAmount").value),
      initial_price: parseFloat(document.getElementById("editInitialPrice").value),
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

    fetch(`/trade/${tradeId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestData),
    })
      .then(res => res.json())
      .then(data => {
        alert(data.message || "Trade updated.");
        bootstrap.Modal.getInstance(document.getElementById("editModal")).hide();
      })
      .catch(err => {
        console.error("Error:", err);
        alert("Failed to update trade.");
      });
  }
});

      wrapper.appendChild(block);

      const priceData = trade.prices.map(p => Number(p.price).toFixed(8));
      const ctx = document.getElementById(chartId).getContext("2d");
      const chart = new Chart(ctx, {
      type: "line",
      data: {
        labels: trade.prices.map(p => p.timestamp),
        datasets: [{
          label: trade.token_name,
          data: trade.prices.map(p => Number(p.price)),
          borderColor: '#4a3aff',
          borderWidth: 2,
          tension: 0.1,
          fill: false
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
        x: {
          ticks: {
            color: getComputedStyle(document.documentElement).getPropertyValue('--text-primary')
          },
          grid: {
            color: 'rgba(255, 255, 255, 0.1)'
          }
        },
        y: {
          ticks: {
            callback: value => Number(value).toFixed(8),
            color: getComputedStyle(document.documentElement).getPropertyValue('--text-primary')
          },
          grid: {
            color: 'rgba(255, 255, 255, 0.1)'
          }
        }
      }
      }
    });

      chartMap[tokenId] = chart;
    });
  });

  // Handle live price updates
  socket.on("price_update", ({ token_address, price }) => {
    const chart = chartMap[token_address];
    if (chart) {
      const data = chart.data.datasets[0].data;
      data.push(price);
      if (data.length > 30) data.shift();
      chart.update();

      const container = document.querySelector(`.container[data-token="${token_address}"]`);
      if (container) {
        container.querySelector(".current-price").textContent = `Current Price(USD): ${price}`;
      }
    }
  });

  // Global button handlers
  document.addEventListener("click", (e) => {
    if (e.target.matches(".sell-enter-btn")) {
      const container = e.target.closest(".trade-card");
      const tokenAddress = e.target.dataset.addr;
      const id = e.target.dataset.id;
      console.log('e',e);
      console.log('id',id);
      const amount = container.querySelector(".sell-amount-input").value;
      if (!amount || isNaN(amount) || parseFloat(amount) <= 0) {
        alert("Please enter a valid amount.");
        amountInput.focus();
        return;
      }
      fetch("/sell_token/" + id +'/', {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tokenAddress, sell_amount: parseFloat(amount), sell_percent:0 })
      });
    }
    if (e.target.matches(".toggle-auto-sell-btn")) {
    const btn = e.target;
    const tradeId = btn.dataset.id;
    const newState = btn.dataset.state === "true" ? false : true;

    fetch(`/api/update_auto_sell/${tradeId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_sell: newState })
    }).then(res => {
      if (res.ok) {
        btn.dataset.state = newState;
        btn.textContent = newState ? "🟢 Auto Sell ON" : "⚪ Auto Sell OFF";
      }
    });
  }
    if (e.target.matches(".buy-enter-btn")) {
      const container = e.target.closest(".trade-card");
      const tokenAddress = e.target.dataset.addr;
      const id = e.target.dataset.id;
      console.log('e',e);
      console.log('id',id);
      const amount = container.querySelector(".buy-amount-input").value;
      if (!amount || isNaN(amount) || parseFloat(amount) <= 0) {
        alert("Please enter a valid amount.");
        amountInput.focus();
        return;
      }

      fetch("/buy_token/" + id +'/', {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tokenAddress, amount: parseFloat(amount) })
      });
    }
    if (e.target.matches(".sell-percentage-btn")) {
      const container = e.target.closest(".trade-card");
      const tokenAddress = e.target.dataset.addr;
      const id = e.target.dataset.id;
      const percent = e.target.dataset.percent;

      fetch("/sell_token/" + id +'/', {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({tokenAddress, sell_percent: parseFloat(percent), sell_amount:0  // ✅ convert to number and rename key to match backend
})
        // body: JSON.stringify({ tokenAddress, percent })
      });
    }

    if (e.target.matches(".time-btn")) {
      const tokenAddress = e.target.dataset.addr;
      const interval = e.target.dataset.interval;

      fetch("/api/set-timer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tokenAddress, interval })
      });

      e.target.closest(".quick-sell-buttons").querySelectorAll(".time-btn").forEach(btn => {
        btn.classList.remove("active");
      });
      e.target.classList.add("active");
    }
  });
  const modal = document.getElementById('tradeModal');
  const closeModalBtn = document.getElementById('closeModalBtn');
  const tbody = document.getElementById('trade-history-body');


  document.addEventListener("click", function (e) {
  if (e.target.classList.contains("history-btn")) {
    const tradeId = e.target.dataset.id;
    const modalEl = document.getElementById(`tradeModal-${tradeId}`);
    const tbody = document.getElementById(`trade-history-body-${tradeId}`);

    console.log("Trade ID:", tradeId);
    if (!modalEl || !tbody) {
      console.error("Modal or tbody not found");
      return;
    }

    tbody.innerHTML = '<tr><td colspan="5">Loading...</td></tr>';

    fetch(`/trade_history/${tradeId}`)
    // fetch(`/trade_history/180`)
      .then(res => {
        if (!res.ok) throw new Error('API error');
        return res.json();
      })
      .then(data => {
        tbody.innerHTML = '';
        const history = data.history || [];

        if (Array.isArray(history) && history.length > 0) {
          history.forEach(trade => {
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

        const modal = new bootstrap.Modal(modalEl);
        modal.show();
      })
      .catch(err => {
        console.error('Fetch error:', err);
        tbody.innerHTML = '<tr><td colspan="5">Failed to load data.</td></tr>';
        const modal = new bootstrap.Modal(modalEl);
        modal.show();
      });
  }
});



