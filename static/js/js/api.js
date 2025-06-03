// api.js

const BASE_URL = ''; // The base URL

// Helper function to make a fetch request
async function makeRequest(url, method, body = null) {
    console.log(url)
    const options = {
        method: method,
        headers: { 'Content-Type': 'application/json' },
    };


    
    if (body) {
        options.body = JSON.stringify(body);
    }

    try {
        const response = await fetch(BASE_URL + url, options);
        const data = await response.json();
        return data;
    } catch (err) {
        console.error(`Error during ${method} request to ${url}:`, err);
        throw err;
    }
}

// GET method
async function getData(apiUrl) {
    return makeRequest(apiUrl, 'GET');
}

// POST method
async function postData(apiUrl, data) {
    return makeRequest(apiUrl, 'POST', data);
}

// PUT method
async function putData(apiUrl, data) {
    return makeRequest(apiUrl, 'PUT', data);
}

// DELETE method
async function deleteData(apiUrl) {
    return makeRequest(apiUrl, 'DELETE');
}

// Export the methods so they can be used in other files
export { getData, postData, putData, deleteData };
