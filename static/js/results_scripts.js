$(document).ready(function() {
    // DataTable Initialization (if there's data)
    if ($('#cernerResultsJson').html() !== 'null' && JSON.parse($('#cernerResultsJson').html()).length > 0) {
        
        // Find the index of the 'Sentence' column
        var sentenceColumnIndex = -1;
        $('#resultsTable thead th').each(function(index) {
            if ($(this).text().trim().toLowerCase() === 'sentence') {
                sentenceColumnIndex = index;
                return false; // break loop
            }
        });

        var dtOptions = {
            "scrollX": true, // Enable horizontal scrolling
            "autoWidth": true   // Let DataTables manage column widths
        };

        if (sentenceColumnIndex !== -1) {
            dtOptions.columnDefs = [
                { "width": "40%", "targets": sentenceColumnIndex } // Suggest 40% width for Sentence column
            ];
        }

        var table = $('#resultsTable').DataTable(dtOptions);
        
        table.columns().every(function() {
            var that = this;
            $('input', this.footer()).on('keyup change clear', function() {
                if (that.search() !== this.value) {
                    that.search(this.value).draw();
                }
            });
        });
    }

    // Tab functionality
    $('.tab-nav a').on('click', function(e) {
        e.preventDefault();
        var targetTab = $(this).attr('href');
        
        // Update active class for tab links
        $('.tab-nav a').removeClass('active');
        $(this).addClass('active');
        
        // Show/hide tab panes
        $('.tab-pane').removeClass('active');
        $(targetTab).addClass('active');
    });

    // Gemini Summary Button Click Handler
    var geminiSummaryGenerated = false;
    $('#generateGeminiSummaryBtn').on('click', function() {
        if (geminiSummaryGenerated) return;
        var cernerData = JSON.parse($('#cernerResultsJson').html());
        var drugName = $("h1").text().replace('Results for "', '').replace('"', '');
        $('#geminiSummaryContent').html('<em>Generating summary, please wait...</em>');
        $.ajax({
            url: '/summarize_cerner_ajax',
            method: 'POST',
            contentType: 'application/json',
            data: JSON.stringify({ drug_name: drugName, cerner_data: cernerData }),
            success: function(response) {
                if (response.summary_html) {
                    $('#geminiSummaryContent').html(response.summary_html);
                    geminiSummaryGenerated = true;
                } else if (response.error) {
                    $('#geminiSummaryContent').html('<span style="color:red;">' + response.error + '</span>');
                }
            },
            error: function(xhr) {
                $('#geminiSummaryContent').html('<span style="color:red;">Error generating summary.</span>');
            }
        });
    });
}); 