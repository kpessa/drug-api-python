document.addEventListener('DOMContentLoaded', function() {
    const cernerForm = document.querySelector('form[action="/search"]');
    // const fullSearchForm = document.querySelector('form[action="/full_drug_search"]'); // Old direct form
    const loadingIndicator = document.getElementById('loading-indicator');

    const getSplSuggestionsBtn = document.getElementById('getSplSuggestionsBtn');
    const splSelectionArea = document.getElementById('splSelectionArea');
    const splCheckboxesContainer = document.getElementById('splCheckboxes');
    const fullDrugNameStep1Input = document.getElementById('full_drug_name_step1');
    const fullDrugNameStep2Input = document.getElementById('full_drug_name_step2');
    const fullDrugSearchStep2Form = document.getElementById('fullDrugSearchStep2Form');
    const splLoadingIndicator = document.getElementById('splLoadingIndicator');
    const splErrorArea = document.getElementById('splErrorArea');

    function showLoading(event) {
        if (loadingIndicator) {
            let drugNameInput = event.target.querySelector('input[name="drug_name"]');
            let drugName = drugNameInput ? drugNameInput.value : "the drug";
            
            if (event.target.id === 'fullDrugSearchStep2Form') { // Check if it's the step 2 form
                loadingIndicator.textContent = `Processing full information request for "${drugName}" with selected forms, please wait... This can take up to a minute or two.`;
            } else if (event.target.getAttribute('action') === '/search') { // Cerner form
                loadingIndicator.textContent = `Processing Cerner search for "${drugName}", please wait...`;
            } else {
                // Generic message for any other form, though not expected now for main loading
                loadingIndicator.textContent = `Processing request for "${drugName}", please wait...`;
            }
            loadingIndicator.style.display = 'block';
        }
    }

    if (cernerForm) {
        cernerForm.addEventListener('submit', showLoading);
    }
    // if (fullSearchForm) { // Old direct form listener removed
    //     fullSearchForm.addEventListener('submit', showLoading);
    // }
    if (fullDrugSearchStep2Form) {
        fullDrugSearchStep2Form.addEventListener('submit', function(event) {
            // Populate hidden drug_name for step 2 form just before submission
            fullDrugNameStep2Input.value = fullDrugNameStep1Input.value;
            
            // Collect selected form names
            const selectedForms = [];
            splCheckboxesContainer.querySelectorAll('input[type="checkbox"]:checked').forEach(function(checkbox) {
                selectedForms.push(checkbox.value);
            });

            if (selectedForms.length === 0) {
                splErrorArea.textContent = 'Please select at least one dosage form to process.';
                splErrorArea.style.display = 'block';
                event.preventDefault(); // Stop form submission
                return;
            }

            // Add selected_form_names as a hidden input to the form before submitting
            let hiddenInput = document.createElement('input');
            hiddenInput.type = 'hidden';
            hiddenInput.name = 'selected_form_names';
            hiddenInput.value = selectedForms.join('_DSF_DELIM_');
            fullDrugSearchStep2Form.appendChild(hiddenInput);

            showLoading(event); // Show the main loading indicator
        });
    }

    if (getSplSuggestionsBtn) {
        getSplSuggestionsBtn.addEventListener('click', function() {
            const drugName = fullDrugNameStep1Input.value.trim();
            if (!drugName) {
                splErrorArea.textContent = 'Please enter a drug name.';
                splErrorArea.style.display = 'block';
                splSelectionArea.style.display = 'none'; 
                return;
            }

            splLoadingIndicator.textContent = `Fetching SPLs and suggestions for "${drugName}"...`;
            splLoadingIndicator.style.display = 'block';
            splErrorArea.style.display = 'none';
            splSelectionArea.style.display = 'none';
            splCheckboxesContainer.innerHTML = ''; // Clear previous checkboxes

            fetch('/fetch_spl_suggestions', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ drug_name: drugName })
            })
            .then(response => {
                splLoadingIndicator.style.display = 'none';
                if (!response.ok) {
                    return response.json().then(err => { throw new Error(err.error || 'Network response was not ok'); });
                }
                return response.json();
            })
            .then(data => {
                if (data.error) {
                    splErrorArea.textContent = data.error;
                    splErrorArea.style.display = 'block';
                    return;
                }
                if (data.all_spls && data.all_spls.length > 0) {
                    fullDrugNameStep2Input.value = drugName; // Store drug name for step 2 form
                    data.all_spls.forEach(function(spl) {
                        const checkboxId = 'spl_checkbox_' + spl.setid + '_' + spl.form_name.replace(/\W/g, '_'); // Sanitize form_name for ID
                        
                        const cardLi = document.createElement('li');
                        cardLi.className = 'spl-card';

                        const cardHeader = document.createElement('div');
                        cardHeader.className = 'spl-card-header';

                        const checkbox = document.createElement('input');
                        checkbox.type = 'checkbox';
                        checkbox.id = checkboxId;
                        checkbox.name = 'selected_spls';
                        checkbox.value = spl.form_name;
                        if (data.prioritized_form_names && data.prioritized_form_names.includes(spl.form_name)) {
                            checkbox.checked = true;
                        }

                        const formNameStrong = document.createElement('strong');
                        formNameStrong.textContent = spl.form_name;

                        cardHeader.appendChild(checkbox);
                        cardHeader.appendChild(formNameStrong);

                        const cardBody = document.createElement('div');
                        cardBody.className = 'spl-card-body';
                        const titleP = document.createElement('p');
                        titleP.className = 'spl-title';
                        titleP.textContent = spl.title;
                        cardBody.appendChild(titleP);
                        const catP = document.createElement('p');
                        catP.textContent = 'Category: ' + spl.marketing_category;
                        cardBody.appendChild(catP);
                        const pubP = document.createElement('p');
                        pubP.textContent = 'Published: ' + spl.published_date;
                        cardBody.appendChild(pubP);
                        const setidP = document.createElement('p');
                        setidP.textContent = 'SETID: ' + spl.setid;
                        cardBody.appendChild(setidP);

                        const cardFooter = document.createElement('div');
                        cardFooter.className = 'spl-card-footer';
                        const pdfLink = document.createElement('a');
                        pdfLink.href = spl.pdf_link;
                        pdfLink.target = '_blank';
                        pdfLink.textContent = 'Download PDF';
                        cardFooter.appendChild(pdfLink);

                        cardLi.appendChild(cardHeader);
                        cardLi.appendChild(cardBody);
                        cardLi.appendChild(cardFooter);

                        splCheckboxesContainer.appendChild(cardLi);
                    });
                    splSelectionArea.style.display = 'block';
                } else {
                    splErrorArea.textContent = 'No SPLs found for this drug.';
                    splErrorArea.style.display = 'block';
                    splSelectionArea.style.display = 'none';
                }
            })
            .catch(error => {
                splLoadingIndicator.style.display = 'none';
                splErrorArea.textContent = error.message || 'An error occurred while fetching SPLs.';
                splErrorArea.style.display = 'block';
                splSelectionArea.style.display = 'none';
            });
        });
    }
}); 