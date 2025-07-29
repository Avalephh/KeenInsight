def search_in_file(file_path, search_term):
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            lines = file.readlines()
        
        matches = []
        for line_number, line in enumerate(lines, start=1):
            if search_term in line:
                matches.append((line_number, line.strip()))
        
        return matches
    except FileNotFoundError:
        print(f"File not found: {file_path}")
        return []

if __name__ == "__main__":
    path = input("Enter the file path: ")
    term = input("Enter the search term: ")
    
    results = search_in_file(path, term)
    
    if results:
        print("Matches found:")
        for line_number, match in results:
            print(f"Line {line_number}: {match}")
    else:
        print("No matches found.")