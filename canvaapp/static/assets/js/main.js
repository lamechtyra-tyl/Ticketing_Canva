/*
 * Change Navbar color while scrolling
*/

$(window).scroll(function(){
	handleTopNavAnimation();
});

$(window).load(function(){
	handleTopNavAnimation();
});

function handleTopNavAnimation() {
	var top=$(window).scrollTop();

	if(top>10){
		$('#site-nav').addClass('navbar-solid'); 
	}
	else{
		$('#site-nav').removeClass('navbar-solid'); 
	}
}

/*
 * Ticket booking and secure payment checkout
*/

(function() {
    var form = document.getElementById('booking-form');
    if (!form) {
        return;
    }

    var message = document.getElementById('booking-message');
    var submitButton = document.getElementById('booking-submit-btn');
    var statusButton = document.getElementById('booking-check-status');
    var resumePayPal = document.getElementById('booking-resume-paypal');
    var buttonLabel = submitButton.querySelector('.button-label');
    var buttonLoading = submitButton.querySelector('.button-loading');
    var priceLabel = document.getElementById('booking-price');
    var phoneGroup = document.getElementById('booking-phone-group');
    var phoneInput = document.getElementById('booking-phone');
    var paymentNote = document.getElementById('booking-payment-note-text');
    var kesPerUsd = Number(priceLabel.dataset.kesPerUsd);
    var csrfToken = form.querySelector('[name="csrfmiddlewaretoken"]').value;
    var activeTicketCode = '';
    var activePaymentMethod = 'mpesa';

    function showMessage(text, kind) {
        message.textContent = text;
        message.className = 'booking-status booking-status-' + kind;
        message.hidden = false;
        message.focus();
    }

    function responseError(data) {
        if (data && typeof data.error === 'string') {
            return data.error;
        }
        if (data && typeof data.detail === 'string') {
            return data.detail;
        }
        if (data && typeof data.message === 'string') {
            return data.message;
        }
        if (data && typeof data === 'object') {
            var keys = Object.keys(data);
            if (keys.length && Array.isArray(data[keys[0]])) {
                return String(data[keys[0]][0]);
            }
            if (keys.length && typeof data[keys[0]] === 'string') {
                return data[keys[0]];
            }
        }
        return 'Something went wrong. Please try again.';
    }

    function requestJson(url, options) {
        options.headers = options.headers || {};
        options.headers['X-CSRFToken'] = csrfToken;
        options.headers.Accept = 'application/json';
        return fetch(url, options).then(function(response) {
            return response.json().catch(function() {
                return {};
            }).then(function(data) {
                if (!response.ok) {
                    var error = new Error(responseError(data));
                    error.status = response.status;
                    throw error;
                }
                return data;
            });
        });
    }

    function pause(milliseconds) {
        return new Promise(function(resolve) {
            window.setTimeout(resolve, milliseconds);
        });
    }

    function selectedValue(name) {
        var selected = form.querySelector('input[name="' + name + '"]:checked');
        return selected ? selected.value : '';
    }

    function formatMoney(amount, currency, digits) {
        var value = Number(amount);
        return Number.isFinite(value)
            ? currency + ' ' + new Intl.NumberFormat('en-KE', {
                minimumFractionDigits: digits,
                maximumFractionDigits: digits
            }).format(value)
            : '';
    }

    function updatePrice() {
        var ticketType = form.querySelector('input[name="ticket_type"]:checked');
        if (!ticketType || !ticketType.dataset.price) {
            priceLabel.textContent = 'Select a ticket category.';
            return;
        }
        var amountKes = Number(ticketType.dataset.price);
        priceLabel.textContent = formatMoney(amountKes, 'KES', 0);
        if (activePaymentMethod === 'paypal' && Number.isFinite(kesPerUsd) && kesPerUsd > 0) {
            priceLabel.textContent += ' · approximately ' +
                formatMoney((amountKes / kesPerUsd).toFixed(2), 'USD', 2) +
                ' at KES ' + kesPerUsd.toFixed(2) + '/USD';
        }
    }

    function updatePaymentMethod() {
        activePaymentMethod = selectedValue('payment_method') || 'mpesa';
        var isPayPal = activePaymentMethod === 'paypal';
        phoneGroup.hidden = isPayPal;
        phoneInput.required = !isPayPal;
        buttonLabel.textContent = isPayPal
            ? 'Get Ticket with PayPal'
            : 'Get Ticket with M-Pesa';
        paymentNote.textContent = isPayPal
            ? 'PayPal uses the displayed USD estimate at the configured exchange rate. Your ticket is confirmed only after PayPal confirms the capture.'
            : 'Your ticket is confirmed only after Safaricom confirms payment.';
        updatePrice();
    }

    function updateStatusControls(data) {
        if (data.payment_method) {
            activePaymentMethod = data.payment_method;
        }
        if (data.approval_url && data.status !== 'paid') {
            resumePayPal.href = data.approval_url;
            resumePayPal.hidden = false;
        } else {
            resumePayPal.hidden = true;
        }
    }

    function resetBookingForm() {
        form.reset();
        activeTicketCode = '';
        activePaymentMethod = 'mpesa';
        statusButton.hidden = true;
        resumePayPal.hidden = true;
        updatePrice();
        updatePaymentMethod();
    }

    function checkPayment(code) {
        var attempts = 0;
        var maxAttempts = 40;

        function poll() {
            attempts += 1;
            return requestJson(
                '/api/tickets/' + encodeURIComponent(code) + '/payment-status/',
                { method: 'GET', cache: 'no-store' }
            ).then(function(data) {
                updateStatusControls(data);
                if (data.status === 'paid') {
                    var reference = data.receipt
                        ? ' M-Pesa receipt: ' + data.receipt + '.'
                        : (data.capture_id ? ' PayPal capture: ' + data.capture_id + '.' : '');
                    showMessage(
                        data.message + ' Ticket ' + data.code +
                        ' is paid.' + reference,
                        'success'
                    );
                    resetBookingForm();
                    return 'paid';
                }
                if (data.status === 'failed') {
                    showMessage(
                        'Payment was not completed. Ticket ' + data.code +
                        ' is not paid. Please try booking again.',
                        'error'
                    );
                    return 'failed';
                }
                if (attempts >= maxAttempts) {
                    showMessage(
                        data.message + ' Ticket ' + code +
                        ' is still pending, not paid. Do not start another payment yet.',
                        'info'
                    );
                    return 'pending';
                }
                return pause(3000).then(poll);
            }).catch(function(error) {
                showMessage(
                    'Could not check the payment status: ' + error.message +
                    ' Ticket ' + code + ' is not confirmed as paid.',
                    'error'
                );
                return 'unknown';
            });
        }

        return poll();
    }

    function capturePayPal(code) {
        return requestJson('/api/paypal/capture/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code: code })
        });
    }

    function refreshPaymentStatus() {
        if (!activeTicketCode) {
            return Promise.resolve('unknown');
        }
        statusButton.disabled = true;
        statusButton.textContent = 'Checking payment status...';
        var capture = activePaymentMethod === 'paypal'
            ? capturePayPal(activeTicketCode).catch(function(error) {
                showMessage(error.message, 'error');
                return null;
            })
            : Promise.resolve();
        return capture.then(function() {
            return checkPayment(activeTicketCode);
        }).then(function(outcome) {
            if (outcome === 'paid' || outcome === 'failed') {
                statusButton.hidden = true;
                submitButton.disabled = false;
                buttonLabel.textContent = activePaymentMethod === 'paypal'
                    ? 'Get Ticket with PayPal'
                    : 'Get Ticket with M-Pesa';
            } else {
                statusButton.disabled = false;
                statusButton.textContent = 'Check payment status again';
            }
            return outcome;
        });
    }

    form.querySelectorAll('input[name="ticket_type"]').forEach(function(input) {
        input.addEventListener('change', updatePrice);
    });
    form.querySelectorAll('input[name="payment_method"]').forEach(function(input) {
        input.addEventListener('change', updatePaymentMethod);
    });

    form.addEventListener('submit', function(event) {
        event.preventDefault();
        if (!form.reportValidity()) {
            return;
        }

        submitButton.disabled = true;
        buttonLabel.hidden = true;
        buttonLoading.hidden = false;
        showMessage('Creating your ticket request…', 'info');

        var ticketCode = '';
        var preventRetry = false;
        var paymentMethod = selectedValue('payment_method');
        var ticketData = {
            event: form.elements.event.value,
            buyer_name: form.elements.buyer_name.value.trim(),
            ticket_type: selectedValue('ticket_type')
        };
        if (paymentMethod === 'mpesa') {
            ticketData.phone = phoneInput.value.trim();
        }

        requestJson('/api/tickets/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(ticketData)
        }).then(function(ticket) {
            ticketCode = ticket.code;
            activeTicketCode = ticketCode;
            activePaymentMethod = paymentMethod;

            if (paymentMethod === 'paypal') {
                showMessage(
                    'Preparing secure PayPal checkout for ' + formatMoney(ticket.price, 'KES', 0) + '…',
                    'info'
                );
                return requestJson('/api/paypal/create-order/', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ code: ticketCode })
                }).then(function(payment) {
                    if (payment.approval_url) {
                        showMessage(
                            'Redirecting to PayPal. Your ticket will only be confirmed after PayPal confirms the payment.',
                            'info'
                        );
                        window.location.assign(payment.approval_url);
                        return 'redirecting';
                    }
                    showMessage(
                        payment.message + ' Ticket ' + ticketCode +
                        ' is pending and is not confirmed as paid.',
                        'info'
                    );
                    return 'pending';
                });
            }

            showMessage('Sending a secure M-Pesa prompt to your phone…', 'info');
            return requestJson('/api/mpesa/stkpush/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ code: ticketCode })
            }).then(function(payment) {
                showMessage(
                    payment.message + ' Enter your M-Pesa PIN on your phone. ' +
                    'Ticket ' + ticketCode + ' will only be confirmed after payment is verified.',
                    'info'
                );
                return checkPayment(ticketCode);
            });
        }).catch(function(error) {
            var ticketNote = ticketCode ? ' Ticket code: ' + ticketCode + '.' : '';
            showMessage(error.message + ticketNote, 'error');
            preventRetry = Boolean(
                ticketCode && (
                    !error.status ||
                    error.status === 409 ||
                    (error.status >= 500 && error.status !== 502 && error.status !== 503)
                )
            );
            return 'error';
        }).then(function(outcome) {
            preventRetry = preventRetry || outcome === 'pending' || outcome === 'unknown';
            if (outcome === 'redirecting') {
                return;
            }
            submitButton.disabled = preventRetry;
            buttonLabel.hidden = false;
            buttonLoading.hidden = true;
            buttonLabel.textContent = preventRetry
                ? 'Payment pending - do not retry yet'
                : paymentMethod === 'paypal'
                    ? 'Get Ticket with PayPal'
                    : 'Get Ticket with M-Pesa';
            if (preventRetry && ticketCode) {
                activeTicketCode = ticketCode;
                statusButton.hidden = false;
            } else {
                statusButton.hidden = true;
            }
        });
    });

    statusButton.addEventListener('click', refreshPaymentStatus);

    var query = new URLSearchParams(window.location.search);
    var paypalResult = query.get('paypal_result');
    var returnedTicketCode = query.get('code');
    if (paypalResult && returnedTicketCode) {
        activeTicketCode = returnedTicketCode;
        activePaymentMethod = 'paypal';
        statusButton.hidden = false;
        submitButton.disabled = true;
        buttonLabel.textContent = 'PayPal checkout in progress';
        if (paypalResult === 'cancelled') {
            showMessage(
                'PayPal checkout was canceled. Your ticket is not paid. You can resume checkout or check its status.',
                'info'
            );
            requestJson(
                '/api/tickets/' + encodeURIComponent(activeTicketCode) + '/payment-status/',
                { method: 'GET', cache: 'no-store' }
            ).then(updateStatusControls).catch(function(error) {
                showMessage('Could not load checkout details: ' + error.message, 'error');
            });
        } else if (paypalResult === 'failed') {
            showMessage(
                'PayPal could not complete the payment. The ticket is not paid. Please try booking again.',
                'error'
            );
            submitButton.disabled = false;
            statusButton.hidden = true;
        } else if (paypalResult === 'error') {
            showMessage(
                'PayPal could not verify this checkout. Your ticket is not confirmed as paid.',
                'error'
            );
            submitButton.disabled = false;
        } else {
            showMessage('Verifying your PayPal payment…', 'info');
            capturePayPal(activeTicketCode).then(function() {
                return checkPayment(activeTicketCode);
            }).then(function(outcome) {
                if (outcome === 'pending' || outcome === 'unknown') {
                    statusButton.hidden = false;
                }
            }).catch(function(error) {
                showMessage('Could not verify PayPal payment: ' + error.message, 'error');
                statusButton.hidden = false;
            });
        }
    } else if (paypalResult === 'error') {
        showMessage(
            'PayPal returned an order that could not be matched to a ticket. Please contact the event team before paying again.',
            'error'
        );
    }

    updatePrice();
    updatePaymentMethod();
})();

/*
 * SmoothScroll
*/

smoothScroll.init();